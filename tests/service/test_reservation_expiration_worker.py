import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest

from inventory_reservation.service.reservation import (
    ExpirationWorkerRunSummary,
    Reservation,
    ReservationExpirationWorker,
    ReservationItem,
)

FIXED_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class StaticClock:
    def now(self) -> datetime:
        return FIXED_NOW


class SequencedExpirationRepository:
    def __init__(self, batches: tuple[tuple[Reservation, ...], ...]) -> None:
        self._batches = deque(batches)
        self.all_batches_processed = asyncio.Event()

    async def expire_batch(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Reservation, ...]:
        batch = self._batches.popleft()
        if not self._batches:
            self.all_batches_processed.set()
        return batch


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
        batch_size=2,
        poll_interval=timedelta(minutes=1),
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
        batch_size=10,
        poll_interval=timedelta(milliseconds=1),
    )

    running_worker = asyncio.create_task(worker.run(stop_event))
    await repository.all_batches_processed.wait()
    stop_event.set()

    assert await running_worker == ExpirationWorkerRunSummary(
        batches_processed=2,
        reservations_processed=0,
    )


def test_worker_rejects_non_positive_batch_size() -> None:
    repository = SequencedExpirationRepository(())

    with pytest.raises(ValueError, match="batch size must be positive"):
        ReservationExpirationWorker(
            transaction_factory=lambda: expiration_transaction(repository),
            clock=StaticClock(),
            batch_size=0,
        )


def test_worker_rejects_non_positive_poll_interval() -> None:
    repository = SequencedExpirationRepository(())

    with pytest.raises(ValueError, match="poll interval must be positive"):
        ReservationExpirationWorker(
            transaction_factory=lambda: expiration_transaction(repository),
            clock=StaticClock(),
            batch_size=10,
            poll_interval=timedelta(0),
        )
