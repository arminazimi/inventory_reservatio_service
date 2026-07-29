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
    PrometheusExpirationWorkerObserver,
)
from inventory_reservation.service.reservation import (
    ExpirationWorkerConfig,
    ExpirationWorkerRunSummary,
    Reservation,
    ReservationExpirationWorker,
    ReservationItem,
    ReservationStatus,
)

FIXED_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class StaticClock:
    def now(self) -> datetime:
        return FIXED_NOW


class SequencedExpirationRepository:
    def __init__(
        self,
        results: tuple[tuple[Reservation, ...] | Exception, ...],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._results = deque(results)
        self._stop_event = stop_event
        self.all_batches_processed = asyncio.Event()

    async def expire_batch(
        self,
        *,
        now: datetime,
        limit: int,
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
async def expiration_transaction(
    repository: SequencedExpirationRepository,
) -> AsyncIterator[SequencedExpirationRepository]:
    yield repository


def reservation() -> Reservation:
    return Reservation(
        id=uuid7(),
        user_id=uuid7(),
        items=(ReservationItem(product_id=uuid7(), quantity=1),),
        idempotency_key=f"expiration-{uuid7()}",
        request_fingerprint="known-fingerprint",
        created_at=FIXED_NOW - timedelta(minutes=16),
        expires_at=FIXED_NOW - timedelta(minutes=1),
    )


async def test_worker_drains_full_batches_and_stops_gracefully() -> None:
    repository = SequencedExpirationRepository(
        (
            (reservation(), reservation()),
            (reservation(),),
        )
    )
    stop_event = asyncio.Event()
    worker = ReservationExpirationWorker(
        transaction_factory=lambda: expiration_transaction(repository),
        clock=StaticClock(),
        config=ExpirationWorkerConfig(
            batch_size=2,
            poll_interval=timedelta(minutes=1),
        ),
    )

    running_worker = asyncio.create_task(worker.run(stop_event))
    await repository.all_batches_processed.wait()
    stop_event.set()

    assert await running_worker == ExpirationWorkerRunSummary(
        batches_processed=2,
        reservations_processed=3,
    )


async def test_worker_polls_again_after_a_short_batch() -> None:
    repository = SequencedExpirationRepository(((), ()))
    stop_event = asyncio.Event()
    worker = ReservationExpirationWorker(
        transaction_factory=lambda: expiration_transaction(repository),
        clock=StaticClock(),
        config=ExpirationWorkerConfig(
            batch_size=10,
            poll_interval=timedelta(milliseconds=1),
        ),
    )

    running_worker = asyncio.create_task(worker.run(stop_event))
    await repository.all_batches_processed.wait()
    stop_event.set()

    assert await running_worker == ExpirationWorkerRunSummary(
        batches_processed=2,
        reservations_processed=0,
    )


async def test_worker_retries_after_a_failed_batch() -> None:
    stop_event = asyncio.Event()
    repository = SequencedExpirationRepository(
        (
            RuntimeError("temporary database failure"),
            (reservation(),),
        ),
        stop_event=stop_event,
    )
    worker = ReservationExpirationWorker(
        transaction_factory=lambda: expiration_transaction(repository),
        clock=StaticClock(),
        config=ExpirationWorkerConfig(
            batch_size=10,
            poll_interval=timedelta(milliseconds=1),
        ),
    )

    assert await worker.run(stop_event) == ExpirationWorkerRunSummary(
        batches_processed=1,
        reservations_processed=1,
        batches_failed=1,
    )


async def test_worker_exports_batch_outcomes_and_reservation_statuses() -> None:
    stop_event = asyncio.Event()
    expired = replace(reservation(), status=ReservationStatus.EXPIRED)
    releasing = replace(reservation(), status=ReservationStatus.RELEASING)
    repository = SequencedExpirationRepository(
        (
            RuntimeError("temporary database failure"),
            (expired, releasing),
        ),
        stop_event=stop_event,
    )
    registry = CollectorRegistry()
    observer = PrometheusExpirationWorkerObserver(registry=registry)
    monotonic_instants = iter((10.0, 11.0, 20.0, 22.0))
    worker = ReservationExpirationWorker(
        transaction_factory=lambda: expiration_transaction(repository),
        clock=StaticClock(),
        config=ExpirationWorkerConfig(
            batch_size=10,
            poll_interval=timedelta(milliseconds=1),
        ),
        observer=observer,
        monotonic_clock=lambda: next(monotonic_instants),
    )

    await worker.run(stop_event)

    metrics = generate_latest(registry).decode()
    assert 'inventory_reservation_expiration_batches_total{outcome="failed"} 1.0' in metrics
    assert 'inventory_reservation_expiration_batches_total{outcome="succeeded"} 1.0' in metrics
    assert 'inventory_reservation_expiration_reservations_total{status="expired"} 1.0' in metrics
    assert 'inventory_reservation_expiration_reservations_total{status="releasing"} 1.0' in metrics


def test_worker_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError, match="batch size must be positive"):
        ExpirationWorkerConfig(
            batch_size=0,
        )


def test_worker_rejects_non_positive_poll_interval() -> None:
    with pytest.raises(ValueError, match="poll interval must be positive"):
        ExpirationWorkerConfig(
            batch_size=10,
            poll_interval=timedelta(0),
        )
