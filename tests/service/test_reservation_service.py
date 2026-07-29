from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest

from inventory_reservation.service.reservation import (
    IdempotencyConflictError,
    Reservation,
    ReservationItem,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=15)
PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000001")
REQUEST_FINGERPRINT = "fe03f417e4461cf79cf202c472aa615af4922ea6f68c61faa6a19c4b529b7e94"


class InMemoryReservationRepository:
    def __init__(self) -> None:
        self._reservations: dict[UUID, Reservation] = {}
        self._reservations_by_key: dict[tuple[UUID, str], Reservation] = {}

    async def add(self, reservation: Reservation) -> None:
        self._reservations[reservation.id] = reservation
        self._reservations_by_key[(reservation.user_id, reservation.idempotency_key)] = reservation

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return self._reservations.get(reservation_id)

    async def get_by_idempotency_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> Reservation | None:
        return self._reservations_by_key.get((user_id, idempotency_key))


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


async def test_created_reservation_is_retrievable() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    item = ReservationItem(product_id=PRODUCT_ID, quantity=2)
    service = ReservationService(
        repository=InMemoryReservationRepository(),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )

    await service.create(
        user_id=user_id,
        items=(item,),
        idempotency_key="checkout-123",
    )

    assert await service.get(reservation_id) == Reservation(
        id=reservation_id,
        user_id=user_id,
        items=(item,),
        idempotency_key="checkout-123",
        request_fingerprint=REQUEST_FINGERPRINT,
        created_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        expires_at=datetime(2026, 7, 29, 12, 15, tzinfo=UTC),
    )


async def test_repeated_create_with_same_key_returns_original_reservation() -> None:
    first_reservation_id = uuid7()
    next_reservation_id = uuid7()
    reservation_ids = iter((first_reservation_id, next_reservation_id))
    user_id = uuid7()
    item = ReservationItem(product_id=PRODUCT_ID, quantity=2)
    service = ReservationService(
        repository=InMemoryReservationRepository(),
        clock=FixedClock(),
        reservation_id_factory=lambda: next(reservation_ids),
        ttl=RESERVATION_TTL,
    )

    first = await service.create(
        user_id=user_id,
        items=(item,),
        idempotency_key="checkout-retry",
    )
    repeated = await service.create(
        user_id=user_id,
        items=(item,),
        idempotency_key="checkout-retry",
    )

    assert (first.id, repeated.id) == (first_reservation_id, first_reservation_id)


async def test_reused_idempotency_key_with_different_payload_is_rejected() -> None:
    user_id = uuid7()
    service = ReservationService(
        repository=InMemoryReservationRepository(),
        clock=FixedClock(),
        reservation_id_factory=uuid7,
        ttl=RESERVATION_TTL,
    )
    await service.create(
        user_id=user_id,
        items=(ReservationItem(product_id=PRODUCT_ID, quantity=2),),
        idempotency_key="checkout-conflict",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.create(
            user_id=user_id,
            items=(ReservationItem(product_id=PRODUCT_ID, quantity=3),),
            idempotency_key="checkout-conflict",
        )
