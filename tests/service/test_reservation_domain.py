from uuid import uuid7

import pytest

from inventory_reservation.service.reservation import (
    EmptyReservationError,
    InvalidReservationQuantityError,
    Reservation,
    ReservationItem,
    ReservationStatus,
)


@pytest.mark.parametrize("quantity", [0, -1])
def test_reservation_item_rejects_non_positive_quantity(quantity: int) -> None:
    with pytest.raises(InvalidReservationQuantityError):
        ReservationItem(product_id=uuid7(), quantity=quantity)


def test_new_reservation_starts_pending() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    item = ReservationItem(product_id=uuid7(), quantity=2)

    reservation = Reservation.start(
        reservation_id=reservation_id,
        user_id=user_id,
        items=(item,),
    )

    assert (
        reservation.id,
        reservation.user_id,
        reservation.items,
        reservation.status,
    ) == (
        reservation_id,
        user_id,
        (item,),
        ReservationStatus.PENDING,
    )


def test_reservation_requires_at_least_one_item() -> None:
    with pytest.raises(EmptyReservationError):
        Reservation.start(
            reservation_id=uuid7(),
            user_id=uuid7(),
            items=(),
        )
