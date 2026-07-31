import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import httpx
import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryProviderModel,
    ProductModel,
    ProductOfferModel,
    ProviderKind,
    ReservationModel,
)
from inventory_reservation.repository.provider import ProviderRegistry
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.service.reservation import (
    InsufficientInventoryError,
    ReconciliationWorkerConfig,
    ReservationItem,
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


@dataclass(frozen=True, slots=True)
class CompensationInventory:
    external_product_id: UUID
    unavailable_product_id: UUID
    external_provider_id: UUID
    internal_provider_id: UUID


async def seed_compensation_inventory(database: Database) -> CompensationInventory:
    async with database.session() as session, session.begin():
        unique_suffix = uuid7().hex
        external_product = ProductModel(
            sku=f"COMPENSATE-EXTERNAL-{unique_suffix}",
            name="Externally held product",
        )
        unavailable_product = ProductModel(
            sku=f"COMPENSATE-UNAVAILABLE-{unique_suffix}",
            name="Unavailable internal product",
        )
        external_provider = InventoryProviderModel(
            name=f"compensate-external-{unique_suffix}",
            kind=ProviderKind.EXTERNAL,
            driver="http",
            base_url="https://inventory-provider.example",
            request_timeout_ms=2000,
            supports_availability=False,
            supports_hold=True,
            supports_release=True,
        )
        internal_provider = InventoryProviderModel(
            name=f"compensate-internal-{unique_suffix}",
            kind=ProviderKind.INTERNAL,
            driver="internal",
            supports_hold=True,
            supports_release=True,
        )
        session.add_all(
            [
                external_product,
                unavailable_product,
                external_provider,
                internal_provider,
            ]
        )
        await session.flush()
        inventory = CompensationInventory(
            external_product_id=external_product.id,
            unavailable_product_id=unavailable_product.id,
            external_provider_id=external_provider.id,
            internal_provider_id=internal_provider.id,
        )
        session.add_all(
            [
                ProductOfferModel(
                    product_id=inventory.external_product_id,
                    provider_id=inventory.external_provider_id,
                    on_hand=0,
                    reserved=0,
                    allocation_priority=1,
                ),
                ProductOfferModel(
                    product_id=inventory.unavailable_product_id,
                    provider_id=inventory.internal_provider_id,
                    on_hand=0,
                    reserved=0,
                    allocation_priority=1,
                ),
            ]
        )
        return inventory


async def delete_compensation_inventory(
    database: Database,
    *,
    reservation_id: UUID,
    inventory: CompensationInventory,
) -> None:
    async with database.session() as session, session.begin():
        await session.execute(delete(ReservationModel).where(ReservationModel.id == reservation_id))
        product_ids = (
            inventory.external_product_id,
            inventory.unavailable_product_id,
        )
        await session.execute(
            delete(ProductOfferModel).where(ProductOfferModel.product_id.in_(product_ids))
        )
        await session.execute(
            delete(InventoryProviderModel).where(
                InventoryProviderModel.id.in_(
                    (
                        inventory.external_provider_id,
                        inventory.internal_provider_id,
                    )
                )
            )
        )
        await session.execute(delete(ProductModel).where(ProductModel.id.in_(product_ids)))


@pytest.mark.integration
@pytest.mark.parametrize("release_timeouts", [0, 2])
async def test_later_item_failure_compensates_an_external_hold_once(
    release_timeouts: int,
) -> None:
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
                json={"hold_reference": "partial-checkout-hold"},
            )
        release_attempts += 1
        if release_attempts <= release_timeouts:
            raise httpx.ReadTimeout(
                "Compensating release timed out",
                request=request,
            )
        return httpx.Response(status_code=204)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)
            inventory = await seed_compensation_inventory(database)

            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )
            items = (
                ReservationItem(
                    product_id=inventory.external_product_id,
                    provider_id=inventory.external_provider_id,
                    quantity=1,
                ),
                ReservationItem(
                    product_id=inventory.unavailable_product_id,
                    provider_id=inventory.internal_provider_id,
                    quantity=1,
                ),
            )

            try:
                with pytest.raises(InsufficientInventoryError):
                    await service.create(
                        user_id=user_id,
                        items=items,
                        idempotency_key="compensated-multi-item-checkout",
                    )
                with pytest.raises(InsufficientInventoryError):
                    await service.create(
                        user_id=user_id,
                        items=items,
                        idempotency_key="compensated-multi-item-checkout",
                    )

                reservation = await service.get(reservation_id)
                assert reservation is not None
                if release_timeouts:
                    assert reservation.status is ReservationStatus.RELEASING
                    assert reservation.release_target_status is ReservationStatus.FAILED

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
                    reconciled = await reconciliation_worker.run_once()
                    assert [item.id for item in reconciled] == [reservation_id]
                    assert await reconciliation_worker.run_once() == ()

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
                    assert [item.id for item in reconciled] == [reservation_id]

                reservation = await service.get(reservation_id)
                assert reservation is not None
                assert reservation.status is ReservationStatus.FAILED
                assert reservation.release_target_status is None
                assert requests == [
                    "/holds",
                    *(["/holds/partial-checkout-hold/release"] * (release_timeouts + 1)),
                ]
            finally:
                await delete_compensation_inventory(
                    database,
                    reservation_id=reservation_id,
                    inventory=inventory,
                )
    finally:
        await database.close()
