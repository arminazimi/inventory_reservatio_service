from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest

from inventory_reservation.service.reservation import (
    IdempotencyConflictError,
    InsufficientInventoryError,
    Reservation,
    ReservationItem,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=15)
PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000001")
PROVIDER_ID = UUID("00000000-0000-7000-8000-000000000002")
REQUEST_FINGERPRINT = "11fcd0a74921195c62ea5421f9308d93976cf5b451740bd3f67173c45a658cd9"


class InMemoryReservationRepository:
    def __init__(self, available_by_product: dict[UUID, int] | None = None) -> None:
        self._reservations: dict[UUID, Reservation] = {}
        self._reservations_by_key: dict[tuple[UUID, str], Reservation] = {}
        self._available_by_product = (
            available_by_product if available_by_product is not None else {PRODUCT_ID: 100}
        )

    async def add(self, reservation: Reservation) -> None:
        self._reservations[reservation.id] = reservation
        self._reservations_by_key[(reservation.user_id, reservation.idempotency_key)] = reservation

    async def add_with_hold(self, reservation: Reservation) -> bool:
        if any(
            self._available_by_product.get(item.product_id, 0) < item.quantity
            for item in reservation.items
        ):
            return False

        for item in reservation.items:
            self._available_by_product[item.product_id] -= item.quantity
        await self.add(reservation)
        return True

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


@asynccontextmanager
async def in_memory_transaction(
    repository: InMemoryReservationRepository,
) -> AsyncIterator[InMemoryReservationRepository]:
    yield repository


async def test_created_reservation_is_retrievable() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    item = ReservationItem(product_id=PRODUCT_ID, provider_id=PROVIDER_ID, quantity=2)
    repository = InMemoryReservationRepository()
    service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
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
    item = ReservationItem(product_id=PRODUCT_ID, provider_id=PROVIDER_ID, quantity=2)
    repository = InMemoryReservationRepository()
    service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
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
    repository = InMemoryReservationRepository()
    service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=uuid7,
        ttl=RESERVATION_TTL,
    )
    await service.create(
        user_id=user_id,
        items=(ReservationItem(product_id=PRODUCT_ID, provider_id=PROVIDER_ID, quantity=2),),
        idempotency_key="checkout-conflict",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.create(
            user_id=user_id,
            items=(ReservationItem(product_id=PRODUCT_ID, provider_id=PROVIDER_ID, quantity=3),),
            idempotency_key="checkout-conflict",
        )


async def test_created_reservation_holds_inventory_for_checkout() -> None:
    repository = InMemoryReservationRepository({PRODUCT_ID: 2})
    service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=uuid7,
        ttl=RESERVATION_TTL,
    )

    await service.create(
        user_id=uuid7(),
        items=(ReservationItem(product_id=PRODUCT_ID, provider_id=PROVIDER_ID, quantity=2),),
        idempotency_key="checkout-holds-stock",
    )

    with pytest.raises(InsufficientInventoryError):
        await service.create(
            user_id=uuid7(),
            items=(ReservationItem(product_id=PRODUCT_ID, provider_id=PROVIDER_ID, quantity=1),),
            idempotency_key="checkout-cannot-reuse-held-stock",
        )
