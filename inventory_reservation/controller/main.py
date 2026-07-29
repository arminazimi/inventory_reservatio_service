import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid7

from fastapi import FastAPI
from starlette.types import Lifespan

from inventory_reservation.controller.reservation import (
    create_reservation_router,
    handle_idempotency_conflict,
)
from inventory_reservation.repository.database import Database
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.service.reservation import (
    IdempotencyConflictError,
    ReservationService,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory"
DEFAULT_RESERVATION_TTL_SECONDS = 900


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def create_app(
    *,
    reservation_service: ReservationService,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Inventory Reservation Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(create_reservation_router(reservation_service))
    app.add_exception_handler(
        IdempotencyConflictError,
        handle_idempotency_conflict,
    )
    return app


def build_app() -> FastAPI:
    database = Database(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    reservation_service = ReservationService(
        transaction_factory=lambda: reservation_transaction(database),
        clock=UtcClock(),
        reservation_id_factory=uuid7,
        ttl=timedelta(
            seconds=int(
                os.getenv(
                    "RESERVATION_TTL_SECONDS",
                    str(DEFAULT_RESERVATION_TTL_SECONDS),
                )
            )
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await database.close()

    return create_app(
        reservation_service=reservation_service,
        lifespan=lifespan,
    )


app = build_app()
