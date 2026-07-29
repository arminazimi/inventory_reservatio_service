from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest

from inventory_reservation.service.reservation import (
    EmptyReservationError,
    InvalidReservationQuantityError,
    InvalidReservationTtlError,
    Reservation,
    ReservationItem,
    ReservationStatus,
)

STARTED_AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=15)


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
        now=STARTED_AT,
        ttl=RESERVATION_TTL,
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


def test_new_reservation_expires_after_ttl() -> None:
    reservation = Reservation.start(
        reservation_id=uuid7(),
        user_id=uuid7(),
        items=(ReservationItem(product_id=uuid7(), quantity=1),),
        now=STARTED_AT,
        ttl=RESERVATION_TTL,
    )

    assert (reservation.created_at, reservation.expires_at) == (
        datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        datetime(2026, 7, 29, 12, 15, tzinfo=UTC),
    )


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_reservation_rejects_non_positive_ttl(ttl: timedelta) -> None:
    with pytest.raises(InvalidReservationTtlError):
        Reservation.start(
            reservation_id=uuid7(),
            user_id=uuid7(),
            items=(ReservationItem(product_id=uuid7(), quantity=1),),
            now=STARTED_AT,
            ttl=ttl,
        )


def test_reservation_requires_at_least_one_item() -> None:
    with pytest.raises(EmptyReservationError):
        Reservation.start(
            reservation_id=uuid7(),
            user_id=uuid7(),
            items=(),
            now=STARTED_AT,
            ttl=RESERVATION_TTL,
        )
