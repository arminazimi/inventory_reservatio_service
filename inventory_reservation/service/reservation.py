from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class InvalidReservationQuantityError(ValueError):
    def __init__(self, quantity: int) -> None:
        super().__init__(f"Reservation item quantity must be positive; got {quantity}")


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
    status: ReservationStatus = field(
        default=ReservationStatus.PENDING,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.items:
            raise EmptyReservationError

    @classmethod
    def start(
        cls,
        *,
        reservation_id: UUID,
        user_id: UUID,
        items: tuple[ReservationItem, ...],
    ) -> Reservation:
        return cls(
            id=reservation_id,
            user_id=user_id,
            items=items,
        )
