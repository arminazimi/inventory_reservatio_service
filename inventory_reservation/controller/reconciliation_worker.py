import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from prometheus_client import CollectorRegistry, start_http_server

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.provider import (
    EnvironmentSecretResolver,
    ProviderRegistry,
)
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.repository.telemetry import (
    PrometheusReconciliationWorkerObserver,
)
from inventory_reservation.service.reservation import (
    ReconciliationWorkerConfig,
    ReconciliationWorkerRunSummary,
    ReservationReconciliationWorker,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory"
DEFAULT_RECONCILIATION_BATCH_SIZE = 100
DEFAULT_RECONCILIATION_MAX_ATTEMPTS = 3
DEFAULT_RECONCILIATION_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RECONCILIATION_RETRY_BASE_DELAY_SECONDS = 30.0
DEFAULT_RECONCILIATION_METRICS_HOST = "0.0.0.0"
DEFAULT_RECONCILIATION_METRICS_PORT = 9102
DEFAULT_PROVIDER_FAILURE_THRESHOLD = 3
DEFAULT_PROVIDER_RECOVERY_TIMEOUT_SECONDS = 30.0


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


async def run_reconciliation_worker() -> ReconciliationWorkerRunSummary:
    structlog.configure(
        processors=(
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        )
    )
    logger = structlog.get_logger("reconciliation_worker")
    database = Database(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    http_client = httpx.AsyncClient()
    try:
        registry = CollectorRegistry()
        observer = PrometheusReconciliationWorkerObserver(registry=registry)
        metrics_host = os.getenv(
            "RECONCILIATION_METRICS_HOST",
            DEFAULT_RECONCILIATION_METRICS_HOST,
        )
        metrics_port = int(
            os.getenv(
                "RECONCILIATION_METRICS_PORT",
                str(DEFAULT_RECONCILIATION_METRICS_PORT),
            )
        )
        metrics_server, metrics_thread = start_http_server(
            metrics_port,
            addr=metrics_host,
            registry=registry,
        )
        provider_registry = ProviderRegistry(
            client=http_client,
            secret_resolver=EnvironmentSecretResolver(),
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
        batch_size = int(
            os.getenv(
                "RECONCILIATION_BATCH_SIZE",
                str(DEFAULT_RECONCILIATION_BATCH_SIZE),
            )
        )
        max_attempts = int(
            os.getenv(
                "RECONCILIATION_MAX_ATTEMPTS",
                str(DEFAULT_RECONCILIATION_MAX_ATTEMPTS),
            )
        )
        poll_interval = timedelta(
            seconds=float(
                os.getenv(
                    "RECONCILIATION_POLL_INTERVAL_SECONDS",
                    str(DEFAULT_RECONCILIATION_POLL_INTERVAL_SECONDS),
                )
            )
        )
        retry_base_delay = timedelta(
            seconds=float(
                os.getenv(
                    "RECONCILIATION_RETRY_BASE_DELAY_SECONDS",
                    str(DEFAULT_RECONCILIATION_RETRY_BASE_DELAY_SECONDS),
                )
            )
        )
        worker = ReservationReconciliationWorker(
            transaction_factory=lambda: reservation_transaction(
                database,
                provider_registry,
            ),
            clock=UtcClock(),
            config=ReconciliationWorkerConfig(
                batch_size=batch_size,
                max_attempts=max_attempts,
                poll_interval=poll_interval,
                retry_base_delay=retry_base_delay,
            ),
            observer=observer,
        )
        stop_event = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
            event_loop.add_signal_handler(shutdown_signal, stop_event.set)

        logger.info(
            "reconciliation_worker_started",
            batch_size=batch_size,
            max_attempts=max_attempts,
            metrics_host=metrics_host,
            metrics_port=metrics_port,
            poll_interval_seconds=poll_interval.total_seconds(),
            retry_base_delay_seconds=retry_base_delay.total_seconds(),
        )
        try:
            summary = await worker.run(stop_event)
            logger.info(
                "reconciliation_worker_stopped",
                batches_failed=summary.batches_failed,
                batches_processed=summary.batches_processed,
                reservations_processed=summary.reservations_processed,
            )
            return summary
        finally:
            metrics_server.shutdown()
            metrics_server.server_close()
            metrics_thread.join()
    finally:
        try:
            await http_client.aclose()
        finally:
            await database.close()


def main() -> None:
    asyncio.run(run_reconciliation_worker())
