import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import httpx
import pytest
from sqlalchemy import delete, select

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory import (
    InventoryRepository,
    InventorySnapshot,
)
from inventory_reservation.repository.models import (
    AllocationStatus,
    InventoryAllocationModel,
    InventoryLevelModel,
    InventoryProviderModel,
    OrderModel,
    ProductModel,
    ProviderKind,
    ProviderOperationModel,
    ProviderOperationStatus,
    ProviderOperationType,
    ReservationItemModel,
    ReservationModel,
)
from inventory_reservation.repository.provider import ProviderRegistry
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.service.reservation import (
    ReconciliationWorkerConfig,
    Reservation,
    ReservationItem,
    ReservationReconciliationRequiredError,
    ReservationReconciliationWorker,
    ReservationService,
    ReservationStatus,
)

FIXED_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class FutureClock:
    def now(self) -> datetime:
        return FIXED_NOW + timedelta(seconds=31)


async def load_external_release_state(
    database: Database,
    *,
    reservation_id: UUID,
) -> tuple[InventoryAllocationModel, ProviderOperationModel]:
    async with database.session() as session:
        allocation = (
            await session.scalars(
                select(InventoryAllocationModel)
                .join(
                    ReservationItemModel,
                    ReservationItemModel.id == InventoryAllocationModel.reservation_item_id,
                )
                .where(ReservationItemModel.reservation_id == reservation_id)
            )
        ).one()
        operation = (
            await session.scalars(
                select(ProviderOperationModel).where(
                    ProviderOperationModel.allocation_id == allocation.id,
                    ProviderOperationModel.operation == ProviderOperationType.RELEASE,
                )
            )
        ).one()
        return allocation, operation


async def assert_external_release_reconciliation(
    database: Database,
    *,
    provider_registry: ProviderRegistry,
    service: ReservationService,
    reservation_id: UUID,
    requests: list[str],
) -> None:
    exhausted_worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=FixedClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=1,
        ),
    )
    assert await exhausted_worker.run_once() == ()
    assert requests == [
        "/holds",
        "/holds/unknown-release-hold/release",
    ]

    reconciliation_worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=FixedClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=3,
            retry_base_delay=timedelta(seconds=30),
        ),
    )
    first_reconciliation = await reconciliation_worker.run_once()
    pending_reservation = await service.get(reservation_id)
    pending_allocation, pending_operation = await load_external_release_state(
        database,
        reservation_id=reservation_id,
    )

    assert [reservation.id for reservation in first_reconciliation] == [reservation_id]
    assert pending_reservation is not None
    assert pending_reservation.status is ReservationStatus.RELEASING
    assert pending_allocation.status is AllocationStatus.UNKNOWN
    assert (
        pending_operation.status,
        pending_operation.attempt_count,
        pending_operation.next_attempt_at,
    ) == (
        ProviderOperationStatus.UNKNOWN,
        2,
        FIXED_NOW + timedelta(seconds=30),
    )
    assert await reconciliation_worker.run_once() == ()
    assert requests == [
        "/holds",
        "/holds/unknown-release-hold/release",
        "/holds/unknown-release-hold/release",
    ]

    due_worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=FutureClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=3,
            retry_base_delay=timedelta(seconds=30),
        ),
    )
    reconciled = await due_worker.run_once()
    reconciled_reservation = await service.get(reservation_id)
    reconciled_allocation, reconciled_operation = await load_external_release_state(
        database,
        reservation_id=reservation_id,
    )

    assert [reservation.id for reservation in reconciled] == [reservation_id]
    assert reconciled_reservation is not None
    assert reconciled_reservation.status is ReservationStatus.CANCELLED
    assert reconciled_allocation.status is AllocationStatus.RELEASED
    assert (
        reconciled_operation.status,
        reconciled_operation.attempt_count,
    ) == (
        ProviderOperationStatus.SUCCEEDED,
        3,
    )
    assert requests == [
        "/holds",
        "/holds/unknown-release-hold/release",
        "/holds/unknown-release-hold/release",
        "/holds/unknown-release-hold/release",
    ]


async def load_external_confirmation_state(
    database: Database,
    *,
    reservation_id: UUID,
) -> tuple[
    InventoryAllocationModel,
    ProviderOperationModel,
    OrderModel | None,
]:
    async with database.session() as session:
        allocation = (
            await session.scalars(
                select(InventoryAllocationModel)
                .join(
                    ReservationItemModel,
                    ReservationItemModel.id == InventoryAllocationModel.reservation_item_id,
                )
                .where(ReservationItemModel.reservation_id == reservation_id)
            )
        ).one()
        operation = (
            await session.scalars(
                select(ProviderOperationModel).where(
                    ProviderOperationModel.allocation_id == allocation.id,
                    ProviderOperationModel.operation == ProviderOperationType.CONFIRM,
                )
            )
        ).one()
        order = (
            await session.scalars(
                select(OrderModel).where(OrderModel.reservation_id == reservation_id)
            )
        ).one_or_none()
        return allocation, operation, order


async def assert_unknown_confirmation_persisted(
    database: Database,
    *,
    reservation_id: UUID,
    first_confirmation: Reservation | None,
    repeated_confirmation: Reservation | None,
    requests: list[str],
) -> None:
    allocation, operation, order = await load_external_confirmation_state(
        database,
        reservation_id=reservation_id,
    )
    assert first_confirmation is not None
    assert repeated_confirmation == first_confirmation
    assert first_confirmation.status is ReservationStatus.CONFIRMING
    assert requests == [
        "/holds",
        "/holds/unknown-confirm-hold/confirm",
    ]
    assert allocation.status is AllocationStatus.UNKNOWN
    assert order is None
    assert (
        operation.status,
        operation.attempt_count,
        operation.external_reference,
    ) == (
        ProviderOperationStatus.UNKNOWN,
        1,
        "unknown-confirm-hold",
    )


async def assert_external_confirmation_reconciliation(
    database: Database,
    *,
    provider_registry: ProviderRegistry,
    service: ReservationService,
    reservation_id: UUID,
    requests: list[str],
) -> None:
    exhausted_worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=FixedClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=1,
        ),
    )
    assert await exhausted_worker.run_once() == ()
    assert requests == [
        "/holds",
        "/holds/unknown-confirm-hold/confirm",
    ]

    reconciliation_worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=FixedClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=3,
            retry_base_delay=timedelta(seconds=30),
        ),
    )
    first_reconciliation = await reconciliation_worker.run_once()
    pending_reservation = await service.get(reservation_id)
    pending_allocation, pending_operation, pending_order = (
        await load_external_confirmation_state(
            database,
            reservation_id=reservation_id,
        )
    )

    assert [reservation.id for reservation in first_reconciliation] == [reservation_id]
    assert pending_reservation is not None
    assert pending_reservation.status is ReservationStatus.CONFIRMING
    assert pending_allocation.status is AllocationStatus.UNKNOWN
    assert pending_order is None
    assert (
        pending_operation.status,
        pending_operation.attempt_count,
        pending_operation.next_attempt_at,
    ) == (
        ProviderOperationStatus.UNKNOWN,
        2,
        FIXED_NOW + timedelta(seconds=30),
    )
    assert await reconciliation_worker.run_once() == ()
    assert requests == [
        "/holds",
        "/holds/unknown-confirm-hold/confirm",
        "/holds/unknown-confirm-hold/confirm",
    ]

    due_worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=FutureClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=3,
            retry_base_delay=timedelta(seconds=30),
        ),
    )
    reconciled = await due_worker.run_once()
    reconciled_reservation = await service.get(reservation_id)
    allocation, operation, order = await load_external_confirmation_state(
        database,
        reservation_id=reservation_id,
    )

    assert [reservation.id for reservation in reconciled] == [reservation_id]
    assert reconciled_reservation is not None
    assert reconciled_reservation.status is ReservationStatus.CONFIRMED
    assert allocation.status is AllocationStatus.CONFIRMED
    assert order is not None
    assert (operation.status, operation.attempt_count) == (
        ProviderOperationStatus.SUCCEEDED,
        3,
    )
    assert requests == [
        "/holds",
        "/holds/unknown-confirm-hold/confirm",
        "/holds/unknown-confirm-hold/confirm",
        "/holds/unknown-confirm-hold/confirm",
    ]


@pytest.mark.integration
async def test_checkout_holds_external_provider_before_internal_fallback() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    external_requests = 0

    async def external_provider_api(_: httpx.Request) -> httpx.Response:
        nonlocal external_requests
        external_requests += 1
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "external-checkout-hold"},
        )

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)

            async with database.session() as session, session.begin():
                unique_suffix = uuid7().hex
                product = ProductModel(
                    sku=f"EXTERNAL-CHECKOUT-{unique_suffix}",
                    name="External checkout test product",
                )
                external_provider = InventoryProviderModel(
                    name=f"external-checkout-{unique_suffix}",
                    kind=ProviderKind.EXTERNAL,
                    driver="http",
                    base_url="https://inventory-provider.example",
                    request_timeout_ms=2000,
                    supports_hold=True,
                )
                internal_provider = InventoryProviderModel(
                    name=f"internal-checkout-fallback-{unique_suffix}",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                session.add_all([product, external_provider, internal_provider])
                await session.flush()
                product_id = product.id
                external_provider_id = external_provider.id
                internal_provider_id = internal_provider.id
                session.add_all(
                    [
                        InventoryLevelModel(
                            product_id=product_id,
                            provider_id=external_provider_id,
                            on_hand=0,
                            reserved=0,
                            allocation_priority=10,
                        ),
                        InventoryLevelModel(
                            product_id=product_id,
                            provider_id=internal_provider_id,
                            on_hand=5,
                            reserved=0,
                            allocation_priority=20,
                        ),
                    ]
                )

            reservation_id = uuid7()
            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )

            try:
                reservation = await service.create(
                    user_id=uuid7(),
                    items=(ReservationItem(product_id=product_id, quantity=2),),
                    idempotency_key="external-provider-checkout",
                )

                async with database.session() as session:
                    internal_inventory = await InventoryRepository(session).get_snapshot(
                        product_id=product_id,
                        provider_id=internal_provider_id,
                    )

                assert (
                    reservation.id,
                    external_requests,
                    internal_inventory,
                ) == (
                    reservation_id,
                    1,
                    InventorySnapshot(
                        product_id=product_id,
                        provider_id=internal_provider_id,
                        on_hand=5,
                        reserved=0,
                        version=1,
                    ),
                )
            finally:
                async with database.session() as session, session.begin():
                    await session.execute(
                        delete(ReservationModel).where(ReservationModel.id == reservation_id)
                    )
                    await session.execute(
                        delete(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id
                        )
                    )
                    await session.execute(
                        delete(InventoryProviderModel).where(
                            InventoryProviderModel.id.in_(
                                (external_provider_id, internal_provider_id)
                            )
                        )
                    )
                    await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()


@pytest.mark.integration
async def test_external_hold_is_confirmed_once_and_operation_is_recorded() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    reservation_id = uuid7()
    user_id = uuid7()
    requests: list[tuple[str, str, str]] = []

    async def external_provider_api(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.path,
                request.headers["Idempotency-Key"],
            )
        )
        if request.url.path == "/holds":
            return httpx.Response(
                status_code=201,
                json={"hold_reference": "external-confirm-hold"},
            )
        return httpx.Response(status_code=204)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)

            async with database.session() as session, session.begin():
                unique_suffix = uuid7().hex
                product = ProductModel(
                    sku=f"EXTERNAL-CONFIRM-{unique_suffix}",
                    name="External confirmation test product",
                )
                provider = InventoryProviderModel(
                    name=f"external-confirm-{unique_suffix}",
                    kind=ProviderKind.EXTERNAL,
                    driver="http",
                    base_url="https://inventory-provider.example",
                    request_timeout_ms=2000,
                    supports_hold=True,
                    supports_confirm=True,
                )
                session.add_all([product, provider])
                await session.flush()
                product_id = product.id
                provider_id = provider.id
                session.add(
                    InventoryLevelModel(
                        product_id=product_id,
                        provider_id=provider_id,
                        on_hand=0,
                        reserved=0,
                    )
                )

            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )

            try:
                await service.create(
                    user_id=user_id,
                    items=(ReservationItem(product_id=product_id, quantity=2),),
                    idempotency_key="external-provider-confirm",
                )
                first_confirmation = await service.confirm(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )
                repeated_confirmation = await service.confirm(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )

                allocation, operation, order = await load_external_confirmation_state(
                    database,
                    reservation_id=reservation_id,
                )

                assert first_confirmation is not None
                assert repeated_confirmation == first_confirmation
                assert first_confirmation.status is ReservationStatus.CONFIRMED
                assert requests == [
                    (
                        "POST",
                        "/holds",
                        f"reservation:{reservation_id}:product:{product_id}:hold",
                    ),
                    (
                        "POST",
                        "/holds/external-confirm-hold/confirm",
                        f"reservation:{reservation_id}:allocation:{allocation.id}:confirm",
                    ),
                ]
                assert allocation.status is AllocationStatus.CONFIRMED
                assert order is not None
                assert (
                    operation.operation,
                    operation.status,
                    operation.idempotency_key,
                    operation.attempt_count,
                    operation.external_reference,
                ) == (
                    ProviderOperationType.CONFIRM,
                    ProviderOperationStatus.SUCCEEDED,
                    f"reservation:{reservation_id}:allocation:{allocation.id}:confirm",
                    1,
                    "external-confirm-hold",
                )
            finally:
                async with database.session() as session, session.begin():
                    await session.execute(
                        delete(OrderModel).where(OrderModel.reservation_id == reservation_id)
                    )
                    await session.execute(
                        delete(ReservationModel).where(ReservationModel.id == reservation_id)
                    )
                    await session.execute(
                        delete(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id
                        )
                    )
                    await session.execute(
                        delete(InventoryProviderModel).where(
                            InventoryProviderModel.id == provider_id
                        )
                    )
                    await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()


@pytest.mark.integration
async def test_unknown_external_confirmation_is_persisted_without_blind_retry() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    reservation_id = uuid7()
    user_id = uuid7()
    requests: list[str] = []
    confirmation_attempts = 0

    async def external_provider_api(request: httpx.Request) -> httpx.Response:
        nonlocal confirmation_attempts
        requests.append(request.url.path)
        if request.url.path == "/holds":
            return httpx.Response(
                status_code=201,
                json={"hold_reference": "unknown-confirm-hold"},
            )
        confirmation_attempts += 1
        if confirmation_attempts <= 2:
            raise httpx.ReadTimeout(
                "Provider confirmation timed out",
                request=request,
            )
        return httpx.Response(status_code=204)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)

            async with database.session() as session, session.begin():
                unique_suffix = uuid7().hex
                product = ProductModel(
                    sku=f"EXTERNAL-UNKNOWN-{unique_suffix}",
                    name="Unknown external confirmation test product",
                )
                provider = InventoryProviderModel(
                    name=f"external-unknown-{unique_suffix}",
                    kind=ProviderKind.EXTERNAL,
                    driver="http",
                    base_url="https://inventory-provider.example",
                    request_timeout_ms=2000,
                    supports_hold=True,
                    supports_confirm=True,
                )
                session.add_all([product, provider])
                await session.flush()
                product_id = product.id
                provider_id = provider.id
                session.add(
                    InventoryLevelModel(
                        product_id=product_id,
                        provider_id=provider_id,
                        on_hand=0,
                        reserved=0,
                    )
                )

            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )

            try:
                await service.create(
                    user_id=user_id,
                    items=(ReservationItem(product_id=product_id, quantity=1),),
                    idempotency_key="unknown-external-confirm",
                )
                first_confirmation = await service.confirm(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )
                repeated_confirmation = await service.confirm(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )
                with pytest.raises(ReservationReconciliationRequiredError):
                    await service.cancel(
                        reservation_id=reservation_id,
                        user_id=user_id,
                    )

                await assert_unknown_confirmation_persisted(
                    database,
                    reservation_id=reservation_id,
                    first_confirmation=first_confirmation,
                    repeated_confirmation=repeated_confirmation,
                    requests=requests,
                )
                await assert_external_confirmation_reconciliation(
                    database,
                    provider_registry=provider_registry,
                    service=service,
                    reservation_id=reservation_id,
                    requests=requests,
                )
            finally:
                async with database.session() as session, session.begin():
                    await session.execute(
                        delete(OrderModel).where(OrderModel.reservation_id == reservation_id)
                    )
                    await session.execute(
                        delete(ReservationModel).where(ReservationModel.id == reservation_id)
                    )
                    await session.execute(
                        delete(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id
                        )
                    )
                    await session.execute(
                        delete(InventoryProviderModel).where(
                            InventoryProviderModel.id == provider_id
                        )
                    )
                    await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()


@pytest.mark.integration
async def test_external_hold_is_released_once_and_operation_is_recorded() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    reservation_id = uuid7()
    user_id = uuid7()
    requests: list[tuple[str, str, str]] = []

    async def external_provider_api(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.path,
                request.headers["Idempotency-Key"],
            )
        )
        if request.url.path == "/holds":
            return httpx.Response(
                status_code=201,
                json={"hold_reference": "external-release-hold"},
            )
        return httpx.Response(status_code=204)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)

            async with database.session() as session, session.begin():
                unique_suffix = uuid7().hex
                product = ProductModel(
                    sku=f"EXTERNAL-RELEASE-{unique_suffix}",
                    name="External release test product",
                )
                provider = InventoryProviderModel(
                    name=f"external-release-{unique_suffix}",
                    kind=ProviderKind.EXTERNAL,
                    driver="http",
                    base_url="https://inventory-provider.example",
                    request_timeout_ms=2000,
                    supports_hold=True,
                    supports_release=True,
                )
                session.add_all([product, provider])
                await session.flush()
                product_id = product.id
                provider_id = provider.id
                session.add(
                    InventoryLevelModel(
                        product_id=product_id,
                        provider_id=provider_id,
                        on_hand=0,
                        reserved=0,
                    )
                )

            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )

            try:
                await service.create(
                    user_id=user_id,
                    items=(ReservationItem(product_id=product_id, quantity=2),),
                    idempotency_key="external-provider-release",
                )
                first_cancellation = await service.cancel(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )
                repeated_cancellation = await service.cancel(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )

                allocation, operation = await load_external_release_state(
                    database,
                    reservation_id=reservation_id,
                )

                assert first_cancellation is not None
                assert repeated_cancellation == first_cancellation
                assert first_cancellation.status is ReservationStatus.CANCELLED
                assert requests == [
                    (
                        "POST",
                        "/holds",
                        f"reservation:{reservation_id}:product:{product_id}:hold",
                    ),
                    (
                        "POST",
                        "/holds/external-release-hold/release",
                        f"reservation:{reservation_id}:allocation:{allocation.id}:release",
                    ),
                ]
                assert allocation.status is AllocationStatus.RELEASED
                assert (
                    operation.status,
                    operation.idempotency_key,
                    operation.attempt_count,
                    operation.external_reference,
                ) == (
                    ProviderOperationStatus.SUCCEEDED,
                    f"reservation:{reservation_id}:allocation:{allocation.id}:release",
                    1,
                    "external-release-hold",
                )
            finally:
                async with database.session() as session, session.begin():
                    await session.execute(
                        delete(ReservationModel).where(ReservationModel.id == reservation_id)
                    )
                    await session.execute(
                        delete(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id
                        )
                    )
                    await session.execute(
                        delete(InventoryProviderModel).where(
                            InventoryProviderModel.id == provider_id
                        )
                    )
                    await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()


@pytest.mark.integration
async def test_unknown_external_release_is_persisted_without_blind_retry() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    reservation_id = uuid7()
    user_id = uuid7()
    requests: list[str] = []
    release_attempts = 0

    async def external_provider_api(request: httpx.Request) -> httpx.Response:
        nonlocal release_attempts
        requests.append(request.url.path)
        if request.url.path == "/holds":
            return httpx.Response(
                status_code=201,
                json={"hold_reference": "unknown-release-hold"},
            )
        release_attempts += 1
        if release_attempts <= 2:
            raise httpx.ReadTimeout(
                "Provider release timed out",
                request=request,
            )
        return httpx.Response(status_code=204)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)

            async with database.session() as session, session.begin():
                unique_suffix = uuid7().hex
                product = ProductModel(
                    sku=f"EXTERNAL-RELEASE-UNKNOWN-{unique_suffix}",
                    name="Unknown external release test product",
                )
                provider = InventoryProviderModel(
                    name=f"external-release-unknown-{unique_suffix}",
                    kind=ProviderKind.EXTERNAL,
                    driver="http",
                    base_url="https://inventory-provider.example",
                    request_timeout_ms=2000,
                    supports_hold=True,
                    supports_release=True,
                )
                session.add_all([product, provider])
                await session.flush()
                product_id = product.id
                provider_id = provider.id
                session.add(
                    InventoryLevelModel(
                        product_id=product_id,
                        provider_id=provider_id,
                        on_hand=0,
                        reserved=0,
                    )
                )

            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )

            try:
                await service.create(
                    user_id=user_id,
                    items=(ReservationItem(product_id=product_id, quantity=1),),
                    idempotency_key="unknown-external-release",
                )
                first_cancellation = await service.cancel(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )
                repeated_cancellation = await service.cancel(
                    reservation_id=reservation_id,
                    user_id=user_id,
                )

                allocation, operation = await load_external_release_state(
                    database,
                    reservation_id=reservation_id,
                )

                assert first_cancellation is not None
                assert repeated_cancellation == first_cancellation
                assert first_cancellation.status is ReservationStatus.RELEASING
                assert requests == [
                    "/holds",
                    "/holds/unknown-release-hold/release",
                ]
                assert allocation.status is AllocationStatus.UNKNOWN
                assert (
                    operation.status,
                    operation.attempt_count,
                    operation.external_reference,
                ) == (
                    ProviderOperationStatus.UNKNOWN,
                    1,
                    "unknown-release-hold",
                )

                await assert_external_release_reconciliation(
                    database,
                    provider_registry=provider_registry,
                    service=service,
                    reservation_id=reservation_id,
                    requests=requests,
                )
            finally:
                async with database.session() as session, session.begin():
                    await session.execute(
                        delete(ReservationModel).where(ReservationModel.id == reservation_id)
                    )
                    await session.execute(
                        delete(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id
                        )
                    )
                    await session.execute(
                        delete(InventoryProviderModel).where(
                            InventoryProviderModel.id == provider_id
                        )
                    )
                    await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()
