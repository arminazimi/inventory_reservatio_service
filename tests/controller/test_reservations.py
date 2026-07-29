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
    def __init__(self) -> None:
        self._reservations_by_id: dict[UUID, Reservation] = {}
        self._reservations_by_key: dict[tuple[UUID, str], Reservation] = {}

    async def add(self, reservation: Reservation) -> None:
        self._reservations_by_id[reservation.id] = reservation
        self._reservations_by_key[(reservation.user_id, reservation.idempotency_key)] = reservation

    async def get(self, reservation_id: UUID) -> Reservation | None:
        return self._reservations_by_id.get(reservation_id)

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
