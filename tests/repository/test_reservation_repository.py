import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory import (
    InventoryRepository,
    InventorySnapshot,
)
from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProductModel,
    ProviderKind,
    ReservationModel,
)
from inventory_reservation.repository.reservation import (
    SqlAlchemyReservationRepository,
    reservation_transaction,
)
from inventory_reservation.service.reservation import (
    InsufficientInventoryError,
    Reservation,
    ReservationItem,
    ReservationRepositoryPort,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class SynchronizingReservationRepository:
    def __init__(
        self,
        repository: ReservationRepositoryPort,
        barrier: asyncio.Barrier,
    ) -> None:
        self._repository = repository
        self._barrier = barrier

    async def add_with_hold(self, reservation: Reservation) -> bool:
        return await self._repository.add_with_hold(reservation)

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return await self._repository.get(reservation_id)

    async def get_by_idempotency_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> Reservation | None:
        reservation = await self._repository.get_by_idempotency_key(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        if reservation is None:
            await self._barrier.wait()
        return reservation


@asynccontextmanager
async def synchronized_reservation_transaction(
    database: Database,
    barrier: asyncio.Barrier,
) -> AsyncIterator[ReservationRepositoryPort]:
    async with reservation_transaction(database) as repository:
        yield SynchronizingReservationRepository(repository, barrier)


@pytest.mark.integration
async def test_added_reservation_is_retrievable_as_domain_aggregate() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        async with database.session() as session:
            try:
                product = ProductModel(
                    sku=f"RESERVATION-ROUNDTRIP-{uuid7().hex}",
                    name="Reservation round-trip test product",
                )
                session.add(product)
                await session.flush()

                reservation = Reservation(
                    id=uuid7(),
                    user_id=uuid7(),
                    items=(ReservationItem(product_id=product.id, quantity=2),),
                    idempotency_key="reservation-roundtrip",
                    request_fingerprint="a" * 64,
                    created_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
                    expires_at=datetime(2026, 7, 29, 12, 15, tzinfo=UTC),
                )
                repository = SqlAlchemyReservationRepository(session)

                await repository.add(reservation)
                await session.flush()
                session.expunge_all()

                assert await repository.get(reservation.id) == reservation
            finally:
                await session.rollback()
    finally:
        await database.close()


@pytest.mark.integration
async def test_concurrent_retries_return_the_same_reservation() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    user_id = uuid7()
    idempotency_key = "postgres-concurrent-retry"

    try:
        async with database.session() as session, session.begin():
            unique_suffix = uuid7().hex
            product = ProductModel(
                sku=f"POSTGRES-CONCURRENT-{unique_suffix}",
                name="PostgreSQL concurrent retry test product",
            )
            provider = InventoryProviderModel(
                name=f"internal-reservation-concurrent-{unique_suffix}",
                kind=ProviderKind.INTERNAL,
                driver="internal",
                supports_hold=True,
            )
            session.add_all([product, provider])
            await session.flush()
            product_id = product.id
            provider_id = provider.id
            session.add(
                InventoryLevelModel(
                    product_id=product_id,
                    provider_id=provider_id,
                    on_hand=2,
                    reserved=0,
                )
            )

        barrier = asyncio.Barrier(2)
        service = ReservationService(
            transaction_factory=lambda: synchronized_reservation_transaction(
                database,
                barrier,
            ),
            clock=FixedClock(),
            reservation_id_factory=uuid7,
            ttl=timedelta(minutes=15),
        )
        item = ReservationItem(product_id=product_id, quantity=2)

        try:
            reservations = await asyncio.gather(
                service.create(
                    user_id=user_id,
                    items=(item,),
                    idempotency_key=idempotency_key,
                ),
                service.create(
                    user_id=user_id,
                    items=(item,),
                    idempotency_key=idempotency_key,
                ),
            )

            async with database.session() as session:
                inventory = await InventoryRepository(session).get_snapshot(
                    product_id=product_id,
                    provider_id=provider_id,
                )

            assert (
                reservations[0].id,
                reservations[1].id,
                inventory,
            ) == (
                reservations[0].id,
                reservations[0].id,
                InventorySnapshot(
                    product_id=product_id,
                    provider_id=provider_id,
                    on_hand=2,
                    reserved=2,
                    version=2,
                ),
            )
        finally:
            async with database.session() as session, session.begin():
                await session.execute(
                    delete(ReservationModel).where(
                        ReservationModel.user_id == user_id,
                        ReservationModel.idempotency_key == idempotency_key,
                    )
                )
                await session.execute(
                    delete(InventoryLevelModel).where(
                        InventoryLevelModel.product_id == product_id,
                        InventoryLevelModel.provider_id == provider_id,
                    )
                )
                await session.execute(
                    delete(InventoryProviderModel).where(InventoryProviderModel.id == provider_id)
                )
                await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()


@pytest.mark.integration
async def test_service_transaction_commits_idempotent_reservation() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        async with database.session() as session, session.begin():
            unique_suffix = uuid7().hex
            product = ProductModel(
                sku=f"POSTGRES-TRANSACTION-{unique_suffix}",
                name="PostgreSQL transaction test product",
            )
            provider = InventoryProviderModel(
                name=f"internal-reservation-transaction-{unique_suffix}",
                kind=ProviderKind.INTERNAL,
                driver="internal",
                supports_hold=True,
            )
            session.add_all([product, provider])
            await session.flush()
            product_id = product.id
            provider_id = provider.id
            session.add(
                InventoryLevelModel(
                    product_id=product_id,
                    provider_id=provider_id,
                    on_hand=2,
                    reserved=0,
                )
            )

        first_reservation_id = uuid7()
        next_reservation_id = uuid7()
        reservation_ids = iter((first_reservation_id, next_reservation_id))
        service = ReservationService(
            transaction_factory=lambda: reservation_transaction(database),
            clock=FixedClock(),
            reservation_id_factory=lambda: next(reservation_ids),
            ttl=timedelta(minutes=15),
        )
        user_id = uuid7()
        item = ReservationItem(product_id=product_id, quantity=2)

        try:
            first = await service.create(
                user_id=user_id,
                items=(item,),
                idempotency_key="postgres-transaction",
            )
            repeated = await service.create(
                user_id=user_id,
                items=(item,),
                idempotency_key="postgres-transaction",
            )
            persisted = await service.get(first_reservation_id)

            assert (first.id, repeated.id, persisted) == (
                first_reservation_id,
                first_reservation_id,
                first,
            )
        finally:
            async with database.session() as session, session.begin():
                await session.execute(
                    delete(ReservationModel).where(ReservationModel.id == first_reservation_id)
                )
                await session.execute(
                    delete(InventoryLevelModel).where(
                        InventoryLevelModel.product_id == product_id,
                        InventoryLevelModel.provider_id == provider_id,
                    )
                )
                await session.execute(
                    delete(InventoryProviderModel).where(InventoryProviderModel.id == provider_id)
                )
                await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()


@pytest.mark.integration
async def test_insufficient_inventory_rolls_back_reservation_and_previous_holds() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    reservation_id = uuid7()

    try:
        async with database.session() as session, session.begin():
            unique_suffix = uuid7().hex
            available_product = ProductModel(
                sku=f"POSTGRES-ROLLBACK-AVAILABLE-{unique_suffix}",
                name="Available rollback test product",
            )
            unavailable_product = ProductModel(
                sku=f"POSTGRES-ROLLBACK-UNAVAILABLE-{unique_suffix}",
                name="Unavailable rollback test product",
            )
            provider = InventoryProviderModel(
                name=f"internal-reservation-rollback-{unique_suffix}",
                kind=ProviderKind.INTERNAL,
                driver="internal",
                supports_hold=True,
            )
            session.add_all([available_product, unavailable_product, provider])
            await session.flush()
            available_product_id = available_product.id
            unavailable_product_id = unavailable_product.id
            provider_id = provider.id
            session.add_all(
                [
                    InventoryLevelModel(
                        product_id=available_product_id,
                        provider_id=provider_id,
                        on_hand=2,
                        reserved=0,
                    ),
                    InventoryLevelModel(
                        product_id=unavailable_product_id,
                        provider_id=provider_id,
                        on_hand=0,
                        reserved=0,
                    ),
                ]
            )

        service = ReservationService(
            transaction_factory=lambda: reservation_transaction(database),
            clock=FixedClock(),
            reservation_id_factory=lambda: reservation_id,
            ttl=timedelta(minutes=15),
        )

        try:
            with pytest.raises(InsufficientInventoryError):
                await service.create(
                    user_id=uuid7(),
                    items=(
                        ReservationItem(
                            product_id=available_product_id,
                            quantity=2,
                        ),
                        ReservationItem(
                            product_id=unavailable_product_id,
                            quantity=1,
                        ),
                    ),
                    idempotency_key="postgres-insufficient-inventory",
                )

            persisted = await service.get(reservation_id)
            async with database.session() as session:
                inventory = await InventoryRepository(session).get_snapshot(
                    product_id=available_product_id,
                    provider_id=provider_id,
                )

            assert persisted is None
            assert inventory == InventorySnapshot(
                product_id=available_product_id,
                provider_id=provider_id,
                on_hand=2,
                reserved=0,
                version=1,
            )
        finally:
            async with database.session() as session, session.begin():
                await session.execute(
                    delete(ReservationModel).where(ReservationModel.id == reservation_id)
                )
                await session.execute(
                    delete(InventoryLevelModel).where(
                        InventoryLevelModel.product_id.in_(
                            (available_product_id, unavailable_product_id)
                        ),
                        InventoryLevelModel.provider_id == provider_id,
                    )
                )
                await session.execute(
                    delete(InventoryProviderModel).where(InventoryProviderModel.id == provider_id)
                )
                await session.execute(
                    delete(ProductModel).where(
                        ProductModel.id.in_((available_product_id, unavailable_product_id))
                    )
                )
    finally:
        await database.close()
