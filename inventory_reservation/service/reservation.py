from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import UUID


class InvalidReservationQuantityError(ValueError):
    def __init__(self, quantity: int) -> None:
        super().__init__(f"Reservation item quantity must be positive; got {quantity}")


class InvalidReservationTtlError(ValueError):
    def __init__(self, ttl: timedelta) -> None:
        super().__init__(f"Reservation TTL must be positive; got {ttl}")


class EmptyReservationError(ValueError):
    def __init__(self) -> None:
        super().__init__("Reservation must contain at least one item")


class ReservationStatus(StrEnum):
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ReservationItem:
    product_id: UUID
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidReservationQuantityError(self.quantity)


@dataclass(frozen=True, slots=True)
class ReservationDraft:
    reservation_id: UUID
    user_id: UUID
    items: tuple[ReservationItem, ...]
    idempotency_key: str
    request_fingerprint: str
    now: datetime
    ttl: timedelta


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    user_id: UUID
    items: tuple[ReservationItem, ...]
    idempotency_key: str
    request_fingerprint: str
    created_at: datetime
    expires_at: datetime
    status: ReservationStatus = field(
        default=ReservationStatus.PENDING,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.items:
            raise EmptyReservationError

        ttl = self.expires_at - self.created_at
        if ttl <= timedelta(0):
            raise InvalidReservationTtlError(ttl)

    @classmethod
    def start(cls, draft: ReservationDraft) -> Reservation:
        return cls(
            id=draft.reservation_id,
            user_id=draft.user_id,
            items=draft.items,
            idempotency_key=draft.idempotency_key,
            request_fingerprint=draft.request_fingerprint,
            created_at=draft.now,
            expires_at=draft.now + draft.ttl,
        )


class Clock(Protocol):
    def now(self) -> datetime: ...


class ReservationRepositoryPort(Protocol):
    async def add(self, reservation: Reservation) -> None: ...

    async def get(self, reservation_id: UUID) -> Reservation | None: ...

    async def get_by_idempotency_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> Reservation | None: ...


class ReservationService:
    def __init__(
        self,
        *,
        repository: ReservationRepositoryPort,
        clock: Clock,
        reservation_id_factory: Callable[[], UUID],
        ttl: timedelta,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._reservation_id_factory = reservation_id_factory
        self._ttl = ttl

    async def create(
        self,
        *,
        user_id: UUID,
        items: tuple[ReservationItem, ...],
        idempotency_key: str,
    ) -> Reservation:
        request_fingerprint = _reservation_request_fingerprint(items)
        existing = await self._repository.get_by_idempotency_key(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing

        reservation = Reservation.start(
            ReservationDraft(
                reservation_id=self._reservation_id_factory(),
                user_id=user_id,
                items=items,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                now=self._clock.now(),
                ttl=self._ttl,
            )
        )
        await self._repository.add(reservation)
        return reservation

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return await self._repository.get(reservation_id)


def _reservation_request_fingerprint(items: tuple[ReservationItem, ...]) -> str:
    canonical_items = "|".join(
        f"{item.product_id}:{item.quantity}"
        for item in sorted(items, key=lambda item: item.product_id.int)
    )
    return sha256(canonical_items.encode()).hexdigest()
