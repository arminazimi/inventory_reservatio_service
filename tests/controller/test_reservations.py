from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.main import create_app
from inventory_reservation.service.reservation import (
    Reservation,
    ReservationRepositoryPort,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
RESERVATION_TTL = timedelta(minutes=15)


class InMemoryReservationRepository:
    def __init__(self, *, inventory_available: bool = True) -> None:
        self._inventory_available = inventory_available
        self._reservations_by_id: dict[UUID, Reservation] = {}
        self._reservations_by_key: dict[tuple[UUID, str], Reservation] = {}

    async def add(self, reservation: Reservation) -> None:
        self._reservations_by_id[reservation.id] = reservation
        self._reservations_by_key[(reservation.user_id, reservation.idempotency_key)] = reservation

    async def add_with_hold(self, reservation: Reservation) -> bool:
        if not self._inventory_available:
            return False
        await self.add(reservation)
        return True

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return self._reservations_by_id.get(reservation_id)

    async def confirm(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None:
        reservation = self._reservations_by_id.get(reservation_id)
        if reservation is None or reservation.user_id != user_id:
            return None

        confirmed = reservation.confirm()
        self._reservations_by_id[reservation_id] = confirmed
        self._reservations_by_key[(confirmed.user_id, confirmed.idempotency_key)] = confirmed
        return confirmed

    async def cancel(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None:
        reservation = self._reservations_by_id.get(reservation_id)
        if reservation is None or reservation.user_id != user_id:
            return None

        cancelled = reservation.cancel()
        self._reservations_by_id[reservation_id] = cancelled
        self._reservations_by_key[(cancelled.user_id, cancelled.idempotency_key)] = cancelled
        return cancelled

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
) -> AsyncIterator[ReservationRepositoryPort]:
    yield repository


async def test_user_can_create_reservation() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/reservations",
            headers={
                "X-User-ID": str(user_id),
                "Idempotency-Key": "checkout-123",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": str(reservation_id),
        "user_id": str(user_id),
        "status": "pending",
        "items": [
            {
                "product_id": str(product_id),
                "quantity": 2,
            }
        ],
        "created_at": "2026-07-29T12:00:00Z",
        "expires_at": "2026-07-29T12:15:00Z",
    }


async def test_create_reservation_returns_conflict_when_inventory_is_insufficient() -> None:
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository(inventory_available=False)
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=uuid7,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/reservations",
            headers={
                "X-User-ID": str(user_id),
                "Idempotency-Key": "checkout-insufficient-inventory",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "insufficient_inventory",
            "message": "Requested inventory is not available.",
        }
    }


async def test_reusing_idempotency_key_for_different_request_returns_conflict() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)
    headers = {
        "X-User-ID": str(user_id),
        "Idempotency-Key": "checkout-conflict",
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers=headers,
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        response = await client.post(
            "/v1/reservations",
            headers=headers,
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 3,
                    }
                ]
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "idempotency_conflict",
            "message": "Idempotency key is already associated with a different request.",
            "reservation_id": str(reservation_id),
        }
    }


async def test_user_can_retrieve_reservation() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers={
                "X-User-ID": str(user_id),
                "Idempotency-Key": "checkout-retrieve",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        response = await client.get(
            f"/v1/reservations/{reservation_id}",
            headers={"X-User-ID": str(user_id)},
        )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(reservation_id),
        "user_id": str(user_id),
        "status": "pending",
        "items": [
            {
                "product_id": str(product_id),
                "quantity": 2,
            }
        ],
        "created_at": "2026-07-29T12:00:00Z",
        "expires_at": "2026-07-29T12:15:00Z",
    }


async def test_user_can_idempotently_confirm_reservation() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)
    headers = {"X-User-ID": str(user_id)}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers={
                **headers,
                "Idempotency-Key": "checkout-confirm",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        first_confirmation = await client.post(
            f"/v1/reservations/{reservation_id}/confirm",
            headers=headers,
        )
        repeated_confirmation = await client.post(
            f"/v1/reservations/{reservation_id}/confirm",
            headers=headers,
        )

    expected_response = {
        "id": str(reservation_id),
        "user_id": str(user_id),
        "status": "confirmed",
        "items": [
            {
                "product_id": str(product_id),
                "quantity": 2,
            }
        ],
        "created_at": "2026-07-29T12:00:00Z",
        "expires_at": "2026-07-29T12:15:00Z",
    }
    assert (
        first_confirmation.status_code,
        first_confirmation.json(),
        repeated_confirmation.status_code,
        repeated_confirmation.json(),
    ) == (
        200,
        expected_response,
        200,
        expected_response,
    )


async def test_user_can_idempotently_cancel_pending_reservation() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)
    headers = {"X-User-ID": str(user_id)}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers={
                **headers,
                "Idempotency-Key": "checkout-cancel",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        first_cancellation = await client.post(
            f"/v1/reservations/{reservation_id}/cancel",
            headers=headers,
        )
        repeated_cancellation = await client.post(
            f"/v1/reservations/{reservation_id}/cancel",
            headers=headers,
        )

    expected_response = {
        "id": str(reservation_id),
        "user_id": str(user_id),
        "status": "cancelled",
        "items": [
            {
                "product_id": str(product_id),
                "quantity": 2,
            }
        ],
        "created_at": "2026-07-29T12:00:00Z",
        "expires_at": "2026-07-29T12:15:00Z",
    }
    assert (
        first_cancellation.status_code,
        first_cancellation.json(),
        repeated_cancellation.status_code,
        repeated_cancellation.json(),
    ) == (
        200,
        expected_response,
        200,
        expected_response,
    )


async def test_confirmed_reservation_cannot_be_cancelled() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)
    headers = {"X-User-ID": str(user_id)}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers={
                **headers,
                "Idempotency-Key": "checkout-confirmed-cancel",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        await client.post(
            f"/v1/reservations/{reservation_id}/confirm",
            headers=headers,
        )
        cancellation = await client.post(
            f"/v1/reservations/{reservation_id}/cancel",
            headers=headers,
        )

    assert cancellation.status_code == 409
    assert cancellation.json() == {
        "error": {
            "code": "reservation_not_cancellable",
            "message": "Confirmed reservation cannot be cancelled.",
            "reservation_id": str(reservation_id),
        }
    }


async def test_cancelled_reservation_cannot_be_confirmed() -> None:
    reservation_id = uuid7()
    user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)
    headers = {"X-User-ID": str(user_id)}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers={
                **headers,
                "Idempotency-Key": "checkout-cancelled-confirm",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        await client.post(
            f"/v1/reservations/{reservation_id}/cancel",
            headers=headers,
        )
        confirmation = await client.post(
            f"/v1/reservations/{reservation_id}/confirm",
            headers=headers,
        )

    assert confirmation.status_code == 409
    assert confirmation.json() == {
        "error": {
            "code": "reservation_not_confirmable",
            "message": "Cancelled reservation cannot be confirmed.",
            "reservation_id": str(reservation_id),
        }
    }


async def test_user_cannot_retrieve_another_users_reservation() -> None:
    reservation_id = uuid7()
    owner_id = uuid7()
    other_user_id = uuid7()
    product_id = uuid7()
    repository = InMemoryReservationRepository()
    reservation_service = ReservationService(
        transaction_factory=lambda: in_memory_transaction(repository),
        clock=FixedClock(),
        reservation_id_factory=lambda: reservation_id,
        ttl=RESERVATION_TTL,
    )
    app = create_app(reservation_service=reservation_service)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post(
            "/v1/reservations",
            headers={
                "X-User-ID": str(owner_id),
                "Idempotency-Key": "checkout-private",
            },
            json={
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            },
        )
        response = await client.get(
            f"/v1/reservations/{reservation_id}",
            headers={"X-User-ID": str(other_user_id)},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "reservation_not_found",
            "message": "Reservation was not found.",
            "reservation_id": str(reservation_id),
        }
    }
