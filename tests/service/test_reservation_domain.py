from uuid import uuid7

import pytest

from inventory_reservation.service.reservation import (
    InvalidReservationQuantityError,
    ReservationItem,
)


@pytest.mark.parametrize("quantity", [0, -1])
def test_reservation_item_rejects_non_positive_quantity(quantity: int) -> None:
    with pytest.raises(InvalidReservationQuantityError):
        ReservationItem(product_id=uuid7(), quantity=quantity)
