import os
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import ProductModel
from inventory_reservation.repository.reservation import (
    SqlAlchemyReservationRepository,
)
from inventory_reservation.service.reservation import (
    Reservation,
    ReservationItem,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


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
async def test_service_retry_returns_original_reservation_from_postgres() -> None:
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
                    sku=f"POSTGRES-RETRY-{uuid7().hex}",
                    name="PostgreSQL retry test product",
                )
                session.add(product)
                await session.flush()

                first_reservation_id = uuid7()
                next_reservation_id = uuid7()
                reservation_ids = iter((first_reservation_id, next_reservation_id))
                service = ReservationService(
                    repository=SqlAlchemyReservationRepository(session),
                    clock=FixedClock(),
                    reservation_id_factory=lambda: next(reservation_ids),
                    ttl=timedelta(minutes=15),
                )
                user_id = uuid7()
                item = ReservationItem(product_id=product.id, quantity=2)

                first = await service.create(
                    user_id=user_id,
                    items=(item,),
                    idempotency_key="postgres-retry",
                )
                repeated = await service.create(
                    user_id=user_id,
                    items=(item,),
                    idempotency_key="postgres-retry",
                )

                assert (first.id, repeated.id) == (
                    first_reservation_id,
                    first_reservation_id,
                )
            finally:
                await session.rollback()
    finally:
        await database.close()
