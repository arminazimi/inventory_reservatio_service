import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderHoldOutcome(StrEnum):
    HELD = "held"
    OUT_OF_STOCK = "out_of_stock"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNKNOWN = "unknown"


class ProviderConfirmOutcome(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HoldCommand:
    product_id: UUID
    quantity: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ConfirmCommand:
    hold_reference: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ProviderHoldAttempt:
    outcome: ProviderHoldOutcome
    reference: str | None = None

    @classmethod
    def held(cls, *, reference: str | None = None) -> ProviderHoldAttempt:
        return cls(
            outcome=ProviderHoldOutcome.HELD,
            reference=reference,
        )

    @classmethod
    def out_of_stock(cls) -> ProviderHoldAttempt:
        return cls(outcome=ProviderHoldOutcome.OUT_OF_STOCK)

    @classmethod
    def temporarily_unavailable(cls) -> ProviderHoldAttempt:
        return cls(outcome=ProviderHoldOutcome.TEMPORARILY_UNAVAILABLE)

    @classmethod
    def unknown(cls) -> ProviderHoldAttempt:
        return cls(outcome=ProviderHoldOutcome.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ProviderConfirmAttempt:
    outcome: ProviderConfirmOutcome

    @classmethod
    def confirmed(cls) -> ProviderConfirmAttempt:
        return cls(outcome=ProviderConfirmOutcome.CONFIRMED)

    @classmethod
    def rejected(cls) -> ProviderConfirmAttempt:
        return cls(outcome=ProviderConfirmOutcome.REJECTED)

    @classmethod
    def temporarily_unavailable(cls) -> ProviderConfirmAttempt:
        return cls(outcome=ProviderConfirmOutcome.TEMPORARILY_UNAVAILABLE)

    @classmethod
    def unknown(cls) -> ProviderConfirmAttempt:
        return cls(outcome=ProviderConfirmOutcome.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ProviderHold:
    provider_id: UUID
    reference: str | None = None


class HoldProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt: ...


class ConfirmProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def confirm(self, command: ConfirmCommand) -> ProviderConfirmAttempt: ...


class InventoryProvider(HoldProvider, ConfirmProvider, Protocol):
    pass


class ProviderCallFailedError(RuntimeError):
    def __init__(self, provider_id: UUID) -> None:
        self.provider_id = provider_id
        super().__init__(f"Provider {provider_id} call failed")


class CircuitBreakerProvider:
    def __init__(
        self,
        *,
        provider: InventoryProvider,
        failure_threshold: int,
        recovery_timeout: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider_id = provider.provider_id
        self._provider = provider
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._monotonic = monotonic
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt:
        is_probe = False
        if self._opened_at is not None:
            if self._monotonic() - self._opened_at < self._recovery_timeout:
                return ProviderHoldAttempt.temporarily_unavailable()
            if self._probe_in_flight:
                return ProviderHoldAttempt.temporarily_unavailable()
            self._probe_in_flight = True
            is_probe = True

        try:
            try:
                attempt = await self._provider.hold(command)
            except ProviderCallFailedError:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._opened_at = self._monotonic()
                return ProviderHoldAttempt.temporarily_unavailable()

            if attempt.outcome is ProviderHoldOutcome.UNKNOWN:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._opened_at = self._monotonic()
                return attempt

            self._failure_count = 0
            self._opened_at = None
            return attempt
        finally:
            if is_probe:
                self._probe_in_flight = False

    async def confirm(self, command: ConfirmCommand) -> ProviderConfirmAttempt:
        is_probe = False
        if self._opened_at is not None:
            if self._monotonic() - self._opened_at < self._recovery_timeout:
                return ProviderConfirmAttempt.temporarily_unavailable()
            if self._probe_in_flight:
                return ProviderConfirmAttempt.temporarily_unavailable()
            self._probe_in_flight = True
            is_probe = True

        try:
            try:
                attempt = await self._provider.confirm(command)
            except ProviderCallFailedError:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._opened_at = self._monotonic()
                return ProviderConfirmAttempt.temporarily_unavailable()

            if attempt.outcome is ProviderConfirmOutcome.UNKNOWN:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._opened_at = self._monotonic()
                return attempt

            self._failure_count = 0
            self._opened_at = None
            return attempt
        finally:
            if is_probe:
                self._probe_in_flight = False


class UnknownProviderOutcomeError(RuntimeError):
    def __init__(
        self,
        *,
        provider_id: UUID,
        idempotency_key: str,
    ) -> None:
        self.provider_id = provider_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Provider {provider_id} returned an unknown outcome for operation {idempotency_key!r}"
        )


class ProviderRouter:
    """Try hold-capable providers in caller-supplied allocation order."""

    def __init__(self, providers: tuple[HoldProvider, ...]) -> None:
        self._providers = providers

    async def hold(self, command: HoldCommand) -> ProviderHold | None:
        for provider in self._providers:
            attempt = await provider.hold(command)
            if attempt.outcome is ProviderHoldOutcome.HELD:
                return ProviderHold(
                    provider_id=provider.provider_id,
                    reference=attempt.reference,
                )
            if attempt.outcome is ProviderHoldOutcome.UNKNOWN:
                raise UnknownProviderOutcomeError(
                    provider_id=provider.provider_id,
                    idempotency_key=command.idempotency_key,
                )

        return None
