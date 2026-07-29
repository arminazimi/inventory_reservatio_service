import os
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import ProductModel, ReservationModel
from inventory_reservation.repository.reservation import (
    SqlAlchemyReservationRepository,
    reservation_transaction,
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
async def test_service_transaction_commits_idempotent_reservation() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        async with database.session() as session, session.begin():
            product = ProductModel(
                sku=f"POSTGRES-TRANSACTION-{uuid7().hex}",
                name="PostgreSQL transaction test product",
            )
            session.add(product)
            await session.flush()
            product_id = product.id

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
                await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()
