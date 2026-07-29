import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from time import monotonic
from typing import Protocol
from uuid import UUID


class InvalidReservationQuantityError(ValueError):
    def __init__(self, quantity: int) -> None:
        super().__init__(f"Reservation item quantity must be positive; got {quantity}")


class InvalidReservationTtlError(ValueError):
    def __init__(self, ttl: timedelta) -> None:
        super().__init__(f"Reservation TTL must be positive; got {ttl}")


class EmptyReservationError(ValueError):
    def __init__(self) -> None:
        super().__init__("Reservation must contain at least one item")


class IdempotencyConflictError(ValueError):
    def __init__(self, idempotency_key: str, reservation_id: UUID) -> None:
        self.idempotency_key = idempotency_key
        self.reservation_id = reservation_id
        super().__init__(
            f"Idempotency key {idempotency_key!r} is already used by reservation "
            f"{reservation_id} for a different request"
        )


class ConcurrentReservationCreationError(RuntimeError):
    """Another transaction created the reservation for this idempotency key."""


class ReservationNotCancellableError(RuntimeError):
    def __init__(self, reservation_id: UUID) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"Confirmed reservation {reservation_id} cannot be cancelled")


class ReservationNotConfirmableError(RuntimeError):
    def __init__(self, reservation_id: UUID) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"Cancelled reservation {reservation_id} cannot be confirmed")


class ReservationReconciliationRequiredError(RuntimeError):
    def __init__(self, reservation_id: UUID) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"Reservation {reservation_id} has an unknown provider outcome")


class ReservationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    RELEASING = "releasing"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReservationItem:
    product_id: UUID
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidReservationQuantityError(self.quantity)


class InsufficientInventoryError(RuntimeError):
    def __init__(self, items: tuple[ReservationItem, ...]) -> None:
        self.items = items
        super().__init__("Insufficient inventory for reservation")


@dataclass(frozen=True, slots=True)
class ReservationDraft:
    reservation_id: UUID
    user_id: UUID
    items: tuple[ReservationItem, ...]
    idempotency_key: str
    request_fingerprint: str
    now: datetime
    ttl: timedelta


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    user_id: UUID
    items: tuple[ReservationItem, ...]
    idempotency_key: str
    request_fingerprint: str
    created_at: datetime
    expires_at: datetime
    status: ReservationStatus = ReservationStatus.PENDING

    def __post_init__(self) -> None:
        if not self.items:
            raise EmptyReservationError

        ttl = self.expires_at - self.created_at
        if ttl <= timedelta(0):
            raise InvalidReservationTtlError(ttl)

    @classmethod
    def start(cls, draft: ReservationDraft) -> Reservation:
        return cls(
            id=draft.reservation_id,
            user_id=draft.user_id,
            items=draft.items,
            idempotency_key=draft.idempotency_key,
            request_fingerprint=draft.request_fingerprint,
            created_at=draft.now,
            expires_at=draft.now + draft.ttl,
        )

    def confirm(self) -> Reservation:
        if self.status is ReservationStatus.CONFIRMED:
            return self
        if self.status is ReservationStatus.CANCELLED:
            raise ReservationNotConfirmableError(self.id)
        return replace(self, status=ReservationStatus.CONFIRMED)

    def cancel(self) -> Reservation:
        if self.status is ReservationStatus.CANCELLED:
            return self
        if self.status is ReservationStatus.CONFIRMED:
            raise ReservationNotCancellableError(self.id)
        if self.status is ReservationStatus.CONFIRMING:
            raise ReservationReconciliationRequiredError(self.id)
        return replace(self, status=ReservationStatus.CANCELLED)


class Clock(Protocol):
    def now(self) -> datetime: ...


class ReservationRepositoryPort(Protocol):
    async def add_with_hold(self, reservation: Reservation) -> bool: ...

    async def confirm(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None: ...

    async def cancel(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None: ...

    async def expire_batch(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Reservation, ...]: ...

    async def reconcile_release_batch(
        self,
        *,
        now: datetime,
        limit: int,
        max_attempts: int,
    ) -> tuple[Reservation, ...]: ...

    async def get(self, reservation_id: UUID) -> Reservation | None: ...

    async def get_by_idempotency_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> Reservation | None: ...


type ReservationTransactionFactory = Callable[
    [],
    AbstractAsyncContextManager[ReservationRepositoryPort],
]


@dataclass(frozen=True, slots=True)
class ExpirationBatchSucceeded:
    reservations: tuple[Reservation, ...]
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ExpirationBatchFailed:
    error: Exception
    duration_seconds: float


type ExpirationBatchObservation = ExpirationBatchSucceeded | ExpirationBatchFailed


class ExpirationWorkerObserver(Protocol):
    def record(self, observation: ExpirationBatchObservation) -> None: ...


class NullExpirationWorkerObserver:
    def record(self, observation: ExpirationBatchObservation) -> None:
        pass


class ReservationService:
    def __init__(
        self,
        *,
        transaction_factory: ReservationTransactionFactory,
        clock: Clock,
        reservation_id_factory: Callable[[], UUID],
        ttl: timedelta,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._clock = clock
        self._reservation_id_factory = reservation_id_factory
        self._ttl = ttl

    async def create(
        self,
        *,
        user_id: UUID,
        items: tuple[ReservationItem, ...],
        idempotency_key: str,
    ) -> Reservation:
        request_fingerprint = _reservation_request_fingerprint(items)

        try:
            async with self._transaction_factory() as repository:
                existing = await repository.get_by_idempotency_key(
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    return _resolve_idempotent_retry(
                        existing,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                    )

                reservation = Reservation.start(
                    ReservationDraft(
                        reservation_id=self._reservation_id_factory(),
                        user_id=user_id,
                        items=items,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        now=self._clock.now(),
                        ttl=self._ttl,
                    )
                )
                inventory_held = await repository.add_with_hold(reservation)
                if not inventory_held:
                    raise InsufficientInventoryError(items)
                return reservation
        except ConcurrentReservationCreationError:
            async with self._transaction_factory() as repository:
                existing = await repository.get_by_idempotency_key(
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                )
            if existing is None:
                raise
            return _resolve_idempotent_retry(
                existing,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )

    async def get(self, reservation_id: UUID) -> Reservation | None:
        async with self._transaction_factory() as repository:
            return await repository.get(reservation_id)

    async def confirm(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None:
        async with self._transaction_factory() as repository:
            return await repository.confirm(
                reservation_id=reservation_id,
                user_id=user_id,
            )

    async def cancel(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None:
        async with self._transaction_factory() as repository:
            return await repository.cancel(
                reservation_id=reservation_id,
                user_id=user_id,
            )


@dataclass(frozen=True, slots=True)
class ExpirationWorkerConfig:
    batch_size: int
    poll_interval: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("Expiration worker batch size must be positive")
        if self.poll_interval <= timedelta(0):
            raise ValueError("Expiration worker poll interval must be positive")


class ReservationExpirationWorker:
    def __init__(
        self,
        *,
        transaction_factory: ReservationTransactionFactory,
        clock: Clock,
        config: ExpirationWorkerConfig,
        observer: ExpirationWorkerObserver | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._clock = clock
        self._config = config
        self._observer = observer if observer is not None else NullExpirationWorkerObserver()
        self._monotonic_clock = monotonic_clock

    async def run_once(self) -> tuple[Reservation, ...]:
        async with self._transaction_factory() as repository:
            return await repository.expire_batch(
                now=self._clock.now(),
                limit=self._config.batch_size,
            )

    async def run(self, stop_event: asyncio.Event) -> ExpirationWorkerRunSummary:
        batches_processed = 0
        reservations_processed = 0
        batches_failed = 0

        while not stop_event.is_set():
            started_at = self._monotonic_clock()
            try:
                reservations = await self.run_once()
            except Exception as error:
                self._observer.record(
                    ExpirationBatchFailed(
                        error=error,
                        duration_seconds=max(0.0, self._monotonic_clock() - started_at),
                    )
                )
                batches_failed += 1
                try:
                    async with asyncio.timeout(self._config.poll_interval.total_seconds()):
                        await stop_event.wait()
                except TimeoutError:
                    pass
                continue

            self._observer.record(
                ExpirationBatchSucceeded(
                    reservations=reservations,
                    duration_seconds=max(0.0, self._monotonic_clock() - started_at),
                )
            )
            batches_processed += 1
            reservations_processed += len(reservations)
            if len(reservations) == self._config.batch_size:
                continue

            try:
                async with asyncio.timeout(self._config.poll_interval.total_seconds()):
                    await stop_event.wait()
            except TimeoutError:
                pass

        return ExpirationWorkerRunSummary(
            batches_processed=batches_processed,
            reservations_processed=reservations_processed,
            batches_failed=batches_failed,
        )


@dataclass(frozen=True, slots=True)
class ExpirationWorkerRunSummary:
    batches_processed: int
    reservations_processed: int
    batches_failed: int = 0


@dataclass(frozen=True, slots=True)
class ReconciliationWorkerConfig:
    batch_size: int
    max_attempts: int

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("Reconciliation worker batch size must be positive")
        if self.max_attempts <= 0:
            raise ValueError("Reconciliation worker max attempts must be positive")


class ReservationReconciliationWorker:
    def __init__(
        self,
        *,
        transaction_factory: ReservationTransactionFactory,
        clock: Clock,
        config: ReconciliationWorkerConfig,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._clock = clock
        self._config = config

    async def run_once(self) -> tuple[Reservation, ...]:
        async with self._transaction_factory() as repository:
            return await repository.reconcile_release_batch(
                now=self._clock.now(),
                limit=self._config.batch_size,
                max_attempts=self._config.max_attempts,
            )


def _reservation_request_fingerprint(items: tuple[ReservationItem, ...]) -> str:
    canonical_items = "|".join(
        f"{item.product_id}:{item.quantity}"
        for item in sorted(items, key=lambda item: item.product_id.int)
    )
    return sha256(canonical_items.encode()).hexdigest()


def _resolve_idempotent_retry(
    existing: Reservation,
    *,
    idempotency_key: str,
    request_fingerprint: str,
) -> Reservation:
    if existing.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(idempotency_key, existing.id)
    return existing
