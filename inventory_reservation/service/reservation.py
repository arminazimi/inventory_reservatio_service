from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
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
class Reservation:
    id: UUID
    user_id: UUID
    items: tuple[ReservationItem, ...]
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
    def start(
        cls,
        *,
        reservation_id: UUID,
        user_id: UUID,
        items: tuple[ReservationItem, ...],
        now: datetime,
        ttl: timedelta,
    ) -> Reservation:
        return cls(
            id=reservation_id,
            user_id=user_id,
            items=items,
            created_at=now,
            expires_at=now + ttl,
        )


class Clock(Protocol):
    def now(self) -> datetime: ...


class ReservationRepositoryPort(Protocol):
    async def add(self, reservation: Reservation) -> None: ...

    async def get(self, reservation_id: UUID) -> Reservation | None: ...


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
    ) -> Reservation:
        reservation = Reservation.start(
            reservation_id=self._reservation_id_factory(),
            user_id=user_id,
            items=items,
            now=self._clock.now(),
            ttl=self._ttl,
        )
        await self._repository.add(reservation)
        return reservation

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return await self._repository.get(reservation_id)
