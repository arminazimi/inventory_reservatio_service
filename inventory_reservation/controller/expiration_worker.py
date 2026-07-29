import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta

import httpx

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.provider import ProviderRegistry
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.service.reservation import (
    ExpirationWorkerRunSummary,
    ReservationExpirationWorker,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory"
DEFAULT_EXPIRATION_BATCH_SIZE = 100
DEFAULT_EXPIRATION_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_PROVIDER_FAILURE_THRESHOLD = 3
DEFAULT_PROVIDER_RECOVERY_TIMEOUT_SECONDS = 30.0


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


async def run_expiration_worker() -> ExpirationWorkerRunSummary:
    database = Database(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    http_client = httpx.AsyncClient()
    provider_registry = ProviderRegistry(
        client=http_client,
        failure_threshold=int(
            os.getenv(
                "PROVIDER_FAILURE_THRESHOLD",
                str(DEFAULT_PROVIDER_FAILURE_THRESHOLD),
            )
        ),
        recovery_timeout=float(
            os.getenv(
                "PROVIDER_RECOVERY_TIMEOUT_SECONDS",
                str(DEFAULT_PROVIDER_RECOVERY_TIMEOUT_SECONDS),
            )
        ),
    )
    worker = ReservationExpirationWorker(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=UtcClock(),
        batch_size=int(
            os.getenv(
                "EXPIRATION_BATCH_SIZE",
                str(DEFAULT_EXPIRATION_BATCH_SIZE),
            )
        ),
        poll_interval=timedelta(
            seconds=float(
                os.getenv(
                    "EXPIRATION_POLL_INTERVAL_SECONDS",
                    str(DEFAULT_EXPIRATION_POLL_INTERVAL_SECONDS),
                )
            )
        ),
    )
    stop_event = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        event_loop.add_signal_handler(shutdown_signal, stop_event.set)

    try:
        return await worker.run(stop_event)
    finally:
        try:
            await http_client.aclose()
        finally:
            await database.close()


def main() -> None:
    asyncio.run(run_expiration_worker())
