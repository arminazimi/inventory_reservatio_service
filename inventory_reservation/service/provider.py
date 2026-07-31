import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable
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


class ProviderReleaseOutcome(StrEnum):
    RELEASED = "released"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNKNOWN = "unknown"


class ProviderAvailabilityOutcome(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"


@dataclass(frozen=True, slots=True)
class AvailabilityCommand:
    product_id: UUID


@dataclass(frozen=True, slots=True)
class ProviderAvailabilityAttempt:
    outcome: ProviderAvailabilityOutcome
    available_quantity: int | None = None
    observed_at: datetime | None = None

    @classmethod
    def fresh(
        cls,
        *,
        available_quantity: int,
        observed_at: datetime | None = None,
    ) -> ProviderAvailabilityAttempt:
        return cls(
            outcome=ProviderAvailabilityOutcome.FRESH,
            available_quantity=available_quantity,
            observed_at=observed_at,
        )

    @classmethod
    def stale(
        cls,
        *,
        available_quantity: int,
        observed_at: datetime | None = None,
    ) -> ProviderAvailabilityAttempt:
        return cls(
            outcome=ProviderAvailabilityOutcome.STALE,
            available_quantity=available_quantity,
            observed_at=observed_at,
        )

    @classmethod
    def temporarily_unavailable(cls) -> ProviderAvailabilityAttempt:
        return cls(outcome=ProviderAvailabilityOutcome.TEMPORARILY_UNAVAILABLE)


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
class ReleaseCommand:
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
class ProviderReleaseAttempt:
    outcome: ProviderReleaseOutcome

    @classmethod
    def released(cls) -> ProviderReleaseAttempt:
        return cls(outcome=ProviderReleaseOutcome.RELEASED)

    @classmethod
    def temporarily_unavailable(cls) -> ProviderReleaseAttempt:
        return cls(outcome=ProviderReleaseOutcome.TEMPORARILY_UNAVAILABLE)

    @classmethod
    def unknown(cls) -> ProviderReleaseAttempt:
        return cls(outcome=ProviderReleaseOutcome.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ProviderHold:
    provider_id: UUID
    reference: str | None = None


class HoldProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt: ...


@runtime_checkable
class AvailabilityProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def availability(
        self,
        command: AvailabilityCommand,
    ) -> ProviderAvailabilityAttempt: ...


class ConfirmProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def confirm(self, command: ConfirmCommand) -> ProviderConfirmAttempt: ...


class ReleaseProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def release(self, command: ReleaseCommand) -> ProviderReleaseAttempt: ...


class InventoryProvider(
    AvailabilityProvider,
    HoldProvider,
    ConfirmProvider,
    ReleaseProvider,
    Protocol,
):
    pass


class SecretResolver(Protocol):
    async def resolve(self, secret_ref: str) -> str: ...


class SecretResolutionError(RuntimeError):
    pass


class ProviderCallFailedError(RuntimeError):
    def __init__(self, provider_id: UUID) -> None:
        self.provider_id = provider_id
        super().__init__(f"Provider {provider_id} call failed")


ProviderAttemptT = TypeVar(
    "ProviderAttemptT",
    ProviderHoldAttempt,
    ProviderConfirmAttempt,
    ProviderReleaseAttempt,
    ProviderAvailabilityAttempt,
)


@dataclass(slots=True)
class _CircuitState:
    failure_count: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


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
        # Availability, hold, confirm, and release have different failure
        # characteristics. A healthy stock read must not close a broken hold
        # circuit (and vice versa).
        self._states = {
            "availability": _CircuitState(),
            "hold": _CircuitState(),
            "confirm": _CircuitState(),
            "release": _CircuitState(),
        }

    async def availability(
        self,
        command: AvailabilityCommand,
    ) -> ProviderAvailabilityAttempt:
        if not isinstance(self._provider, AvailabilityProvider):
            return ProviderAvailabilityAttempt.stale(  # type: ignore[unreachable]
                available_quantity=0
            )
        return await self._execute(
            state=self._states["availability"],
            operation=lambda: self._provider.availability(command),
            temporarily_unavailable=ProviderAvailabilityAttempt.temporarily_unavailable,
            is_unknown=lambda _: False,
        )

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt:
        return await self._execute(
            state=self._states["hold"],
            operation=lambda: self._provider.hold(command),
            temporarily_unavailable=ProviderHoldAttempt.temporarily_unavailable,
            is_unknown=lambda attempt: attempt.outcome is ProviderHoldOutcome.UNKNOWN,
        )

    async def confirm(self, command: ConfirmCommand) -> ProviderConfirmAttempt:
        return await self._execute(
            state=self._states["confirm"],
            operation=lambda: self._provider.confirm(command),
            temporarily_unavailable=ProviderConfirmAttempt.temporarily_unavailable,
            is_unknown=lambda attempt: attempt.outcome is ProviderConfirmOutcome.UNKNOWN,
        )

    async def release(self, command: ReleaseCommand) -> ProviderReleaseAttempt:
        return await self._execute(
            state=self._states["release"],
            operation=lambda: self._provider.release(command),
            temporarily_unavailable=ProviderReleaseAttempt.temporarily_unavailable,
            is_unknown=lambda attempt: attempt.outcome is ProviderReleaseOutcome.UNKNOWN,
        )

    async def _execute(
        self,
        *,
        state: _CircuitState,
        operation: Callable[[], Awaitable[ProviderAttemptT]],
        temporarily_unavailable: Callable[[], ProviderAttemptT],
        is_unknown: Callable[[ProviderAttemptT], bool],
    ) -> ProviderAttemptT:
        is_probe = False
        if state.opened_at is not None:
            if self._monotonic() - state.opened_at < self._recovery_timeout:
                return temporarily_unavailable()
            if state.probe_in_flight:
                return temporarily_unavailable()
            state.probe_in_flight = True
            is_probe = True

        try:
            try:
                attempt = await operation()
            except ProviderCallFailedError:
                state.failure_count += 1
                if state.failure_count >= self._failure_threshold:
                    state.opened_at = self._monotonic()
                return temporarily_unavailable()

            if is_unknown(attempt):
                state.failure_count += 1
                if state.failure_count >= self._failure_threshold:
                    state.opened_at = self._monotonic()
                return attempt

            state.failure_count = 0
            state.opened_at = None
            return attempt
        finally:
            if is_probe:
                state.probe_in_flight = False


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

    def __init__(
        self,
        providers: tuple[HoldProvider, ...],
        *,
        availability_provider_ids: frozenset[UUID] | None = None,
        availability_observer: Callable[
            [UUID, ProviderAvailabilityAttempt], Awaitable[None]
        ]
        | None = None,
    ) -> None:
        self._providers = providers
        self._availability_provider_ids = (
            availability_provider_ids
            if availability_provider_ids is not None
            else frozenset(
                provider.provider_id
                for provider in providers
                if isinstance(provider, AvailabilityProvider)
            )
        )
        self._availability_observer = availability_observer

    async def hold(self, command: HoldCommand) -> ProviderHold | None:
        for provider in self._providers:
            if (
                provider.provider_id in self._availability_provider_ids
                and isinstance(provider, AvailabilityProvider)
            ):
                availability = await provider.availability(
                    AvailabilityCommand(product_id=command.product_id)
                )
                if self._availability_observer is not None:
                    await self._availability_observer(
                        provider.provider_id,
                        availability,
                    )
                if (
                    availability.outcome
                    is ProviderAvailabilityOutcome.TEMPORARILY_UNAVAILABLE
                ):
                    continue
                if (
                    availability.outcome is ProviderAvailabilityOutcome.FRESH
                    and availability.available_quantity is not None
                    and availability.available_quantity < command.quantity
                ):
                    continue
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
