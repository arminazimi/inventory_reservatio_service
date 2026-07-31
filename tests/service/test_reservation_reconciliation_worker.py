import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from inventory_reservation.repository.telemetry import (
    PrometheusReconciliationWorkerObserver,
)
from inventory_reservation.service.reservation import (
    ReconciliationWorkerConfig,
    ReconciliationWorkerRunSummary,
    Reservation,
    ReservationItem,
    ReservationReconciliationWorker,
    ReservationStatus,
)

FIXED_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class StaticClock:
    def now(self) -> datetime:
        return FIXED_NOW


class SequencedReconciliationRepository:
    def __init__(
        self,
        results: tuple[tuple[Reservation, ...] | Exception, ...],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._results = deque(results)
        self._stop_event = stop_event
        self.all_batches_processed = asyncio.Event()

    async def reconcile_batch(
        self,
        *,
        now: datetime,
        limit: int,
        max_attempts: int,
        retry_base_delay: timedelta,
    ) -> tuple[Reservation, ...]:
        result = self._results.popleft()
        if not self._results:
            self.all_batches_processed.set()
            if self._stop_event is not None:
                self._stop_event.set()
        if isinstance(result, Exception):
            raise result
        return result


@asynccontextmanager
async def reconciliation_transaction(
    repository: SequencedReconciliationRepository,
) -> AsyncIterator[SequencedReconciliationRepository]:
    yield repository


def reservation() -> Reservation:
    return Reservation(
        id=uuid7(),
        user_id=uuid7(),
        items=(ReservationItem(provider_id=uuid7(), product_id=uuid7(), quantity=1),),
        idempotency_key=f"reconciliation-{uuid7()}",
        request_fingerprint="known-fingerprint",
        created_at=FIXED_NOW - timedelta(minutes=1),
        expires_at=FIXED_NOW + timedelta(minutes=14),
    )


async def test_worker_drains_full_batches_and_stops_gracefully() -> None:
    repository = SequencedReconciliationRepository(
        (
            (reservation(), reservation()),
            (reservation(),),
        )
    )
    stop_event = asyncio.Event()
    worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reconciliation_transaction(repository),
        clock=StaticClock(),
        config=ReconciliationWorkerConfig(
            batch_size=2,
            max_attempts=3,
            poll_interval=timedelta(minutes=1),
            retry_base_delay=timedelta(seconds=30),
        ),
    )

    running_worker = asyncio.create_task(worker.run(stop_event))
    await repository.all_batches_processed.wait()
    stop_event.set()

    assert await running_worker == ReconciliationWorkerRunSummary(
        batches_processed=2,
        reservations_processed=3,
    )


async def test_worker_retries_after_a_failed_batch() -> None:
    stop_event = asyncio.Event()
    repository = SequencedReconciliationRepository(
        (
            RuntimeError("temporary database failure"),
            (reservation(),),
        ),
        stop_event=stop_event,
    )
    worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reconciliation_transaction(repository),
        clock=StaticClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=3,
            poll_interval=timedelta(milliseconds=1),
        ),
    )

    assert await worker.run(stop_event) == ReconciliationWorkerRunSummary(
        batches_processed=1,
        reservations_processed=1,
        batches_failed=1,
    )


async def test_worker_exports_batch_outcomes_and_reservation_statuses() -> None:
    stop_event = asyncio.Event()
    confirmed = replace(reservation(), status=ReservationStatus.CONFIRMED)
    releasing = replace(reservation(), status=ReservationStatus.RELEASING)
    repository = SequencedReconciliationRepository(
        (
            RuntimeError("temporary database failure"),
            (confirmed, releasing),
        ),
        stop_event=stop_event,
    )
    registry = CollectorRegistry()
    observer = PrometheusReconciliationWorkerObserver(registry=registry)
    monotonic_instants = iter((10.0, 11.0, 20.0, 22.0))
    worker = ReservationReconciliationWorker(
        transaction_factory=lambda: reconciliation_transaction(repository),
        clock=StaticClock(),
        config=ReconciliationWorkerConfig(
            batch_size=10,
            max_attempts=3,
            poll_interval=timedelta(milliseconds=1),
        ),
        observer=observer,
        monotonic_clock=lambda: next(monotonic_instants),
    )

    await worker.run(stop_event)

    metrics = generate_latest(registry).decode()
    assert 'inventory_reservation_reconciliation_batches_total{outcome="failed"} 1.0' in metrics
    assert 'inventory_reservation_reconciliation_batches_total{outcome="succeeded"} 1.0' in metrics
    assert (
        'inventory_reservation_reconciliation_reservations_total{status="confirmed"} 1.0' in metrics
    )
    assert (
        'inventory_reservation_reconciliation_reservations_total{status="releasing"} 1.0' in metrics
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"batch_size": 0}, "batch size must be positive"),
        ({"max_attempts": 0}, "max attempts must be positive"),
        ({"poll_interval": timedelta(0)}, "poll interval must be positive"),
        ({"retry_base_delay": timedelta(0)}, "retry base delay must be positive"),
    ],
)
def test_worker_rejects_non_positive_configuration(
    overrides: dict[str, int | timedelta],
    message: str,
) -> None:
    config = {
        "batch_size": 10,
        "max_attempts": 3,
        "poll_interval": timedelta(seconds=5),
        "retry_base_delay": timedelta(seconds=30),
        **overrides,
    }

    with pytest.raises(ValueError, match=message):
        ReconciliationWorkerConfig(**config)
