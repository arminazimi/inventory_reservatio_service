from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderHoldOutcome(StrEnum):
    HELD = "held"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HoldCommand:
    product_id: UUID
    quantity: int
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
    def unknown(cls) -> ProviderHoldAttempt:
        return cls(outcome=ProviderHoldOutcome.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ProviderHold:
    provider_id: UUID
    reference: str | None = None


class HoldProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt: ...


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
