from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from inventory_reservation.service.reservation import (
    Reservation,
    ReservationItem,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=15)


class InMemoryReservationRepository:
    def __init__(self) -> None:
        self._reservations: dict[UUID, Reservation] = {}

    async def add(self, reservation: Reservation) -> None:
        self._reservations[reservation.id] = reservation

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return self._reservations.get(reservation_id)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


async def test_created_reservation_is_retrievable() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    item = ReservationItem(product_id=uuid7(), quantity=2)
    service = ReservationService(
        repository=InMemoryReservationRepository(),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )

    await service.create(user_id=user_id, items=(item,))

    assert await service.get(reservation_id) == Reservation(
        id=reservation_id,
        user_id=user_id,
        items=(item,),
        created_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        expires_at=datetime(2026, 7, 29, 12, 15, tzinfo=UTC),
    )
