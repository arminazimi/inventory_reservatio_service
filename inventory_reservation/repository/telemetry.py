from time import monotonic
from typing import cast

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import FilteringBoundLogger

from inventory_reservation.service.reservation import (
    ExpirationBatchFailed,
    ExpirationBatchObservation,
    ExpirationBatchSucceeded,
    ExpirationWorkerObserver,
    ReconciliationBatchFailed,
    ReconciliationBatchObservation,
    ReconciliationBatchSucceeded,
    ReconciliationWorkerObserver,
)


class PrometheusHttpMetrics:
    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: CollectorRegistry,
    ) -> None:
        self._app = app
        self._requests = Counter(
            "inventory_reservation_http_requests_total",
            "HTTP requests processed by route and response status.",
            ("method", "route", "status"),
            registry=registry,
        )
        self._request_duration = Histogram(
            "inventory_reservation_http_request_duration_seconds",
            "HTTP request duration by method and route.",
            ("method", "route"),
            registry=registry,
        )
        self._in_progress = Gauge(
            "inventory_reservation_http_requests_in_progress",
            "HTTP requests currently being processed by method.",
            ("method",),
            registry=registry,
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self._app(scope, receive, send)
            return

        method = scope["method"]
        status_code = 500
        started_at = monotonic()
        self._in_progress.labels(method=method).inc()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, send_with_status)
        finally:
            route = cast(str, getattr(scope.get("route"), "path", "unmatched"))
            self._in_progress.labels(method=method).dec()
            self._requests.labels(
                method=method,
                route=route,
                status=str(status_code),
            ).inc()
            self._request_duration.labels(
                method=method,
                route=route,
            ).observe(monotonic() - started_at)


class PrometheusExpirationWorkerObserver(ExpirationWorkerObserver):
    def __init__(
        self,
        *,
        registry: CollectorRegistry,
        logger: FilteringBoundLogger | None = None,
    ) -> None:
        self._logger = (
            logger
            if logger is not None
            else cast(FilteringBoundLogger, structlog.get_logger("expiration_worker"))
        )
        self._batches = Counter(
            "inventory_reservation_expiration_batches_total",
            "Reservation expiration batches processed by outcome.",
            ("outcome",),
            registry=registry,
        )
        self._reservations = Counter(
            "inventory_reservation_expiration_reservations_total",
            "Reservations processed by the expiration worker and resulting status.",
            ("status",),
            registry=registry,
        )
        self._batch_duration = Histogram(
            "inventory_reservation_expiration_batch_duration_seconds",
            "Time spent processing one reservation expiration batch.",
            ("outcome",),
            registry=registry,
        )
        self._last_success = Gauge(
            "inventory_reservation_expiration_last_success_unixtime",
            "Unix timestamp of the last successful expiration batch.",
            registry=registry,
        )

    def record(self, observation: ExpirationBatchObservation) -> None:
        if isinstance(observation, ExpirationBatchSucceeded):
            self._record_success(observation)
            return
        self._record_failure(observation)

    def _record_success(self, observation: ExpirationBatchSucceeded) -> None:
        self._batches.labels(outcome="succeeded").inc()
        self._batch_duration.labels(outcome="succeeded").observe(observation.duration_seconds)
        self._last_success.set_to_current_time()
        status_counts: dict[str, int] = {}
        for reservation in observation.reservations:
            status = reservation.status.value
            self._reservations.labels(status=status).inc()
            status_counts[status] = status_counts.get(status, 0) + 1
        self._logger.info(
            "expiration_batch_completed",
            duration_seconds=observation.duration_seconds,
            reservations_processed=len(observation.reservations),
            reservation_statuses=status_counts,
        )

    def _record_failure(self, observation: ExpirationBatchFailed) -> None:
        self._batches.labels(outcome="failed").inc()
        self._batch_duration.labels(outcome="failed").observe(observation.duration_seconds)
        self._logger.error(
            "expiration_batch_failed",
            duration_seconds=observation.duration_seconds,
            error_type=type(observation.error).__name__,
            error=str(observation.error),
        )


class PrometheusReconciliationWorkerObserver(ReconciliationWorkerObserver):
    def __init__(
        self,
        *,
        registry: CollectorRegistry,
        logger: FilteringBoundLogger | None = None,
    ) -> None:
        self._logger = (
            logger
            if logger is not None
            else cast(FilteringBoundLogger, structlog.get_logger("reconciliation_worker"))
        )
        self._batches = Counter(
            "inventory_reservation_reconciliation_batches_total",
            "Reservation reconciliation batches processed by outcome.",
            ("outcome",),
            registry=registry,
        )
        self._reservations = Counter(
            "inventory_reservation_reconciliation_reservations_total",
            "Reservations processed by the reconciliation worker and resulting status.",
            ("status",),
            registry=registry,
        )
        self._batch_duration = Histogram(
            "inventory_reservation_reconciliation_batch_duration_seconds",
            "Time spent processing one reservation reconciliation batch.",
            ("outcome",),
            registry=registry,
        )
        self._last_success = Gauge(
            "inventory_reservation_reconciliation_last_success_unixtime",
            "Unix timestamp of the last successful reconciliation batch.",
            registry=registry,
        )

    def record(self, observation: ReconciliationBatchObservation) -> None:
        if isinstance(observation, ReconciliationBatchSucceeded):
            self._record_success(observation)
            return
        self._record_failure(observation)

    def _record_success(self, observation: ReconciliationBatchSucceeded) -> None:
        self._batches.labels(outcome="succeeded").inc()
        self._batch_duration.labels(outcome="succeeded").observe(
            observation.duration_seconds
        )
        self._last_success.set_to_current_time()
        status_counts: dict[str, int] = {}
        for reservation in observation.reservations:
            status = reservation.status.value
            self._reservations.labels(status=status).inc()
            status_counts[status] = status_counts.get(status, 0) + 1
        self._logger.info(
            "reconciliation_batch_completed",
            duration_seconds=observation.duration_seconds,
            reservations_processed=len(observation.reservations),
            reservation_statuses=status_counts,
        )

    def _record_failure(self, observation: ReconciliationBatchFailed) -> None:
        self._batches.labels(outcome="failed").inc()
        self._batch_duration.labels(outcome="failed").observe(
            observation.duration_seconds
        )
        self._logger.error(
            "reconciliation_batch_failed",
            duration_seconds=observation.duration_seconds,
            error_type=type(observation.error).__name__,
            error=str(observation.error),
        )
