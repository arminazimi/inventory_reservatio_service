import os
from datetime import UTC, datetime
from uuid import uuid7

import pytest

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import ProductModel
from inventory_reservation.repository.reservation import (
    SqlAlchemyReservationRepository,
)
from inventory_reservation.service.reservation import Reservation, ReservationItem


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
