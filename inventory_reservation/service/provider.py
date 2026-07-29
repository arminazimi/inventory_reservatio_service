from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderHoldOutcome(StrEnum):
    HELD = "held"
    OUT_OF_STOCK = "out_of_stock"


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


@dataclass(frozen=True, slots=True)
class ProviderHold:
    provider_id: UUID
    reference: str | None = None


class HoldProvider(Protocol):
    @property
    def provider_id(self) -> UUID: ...

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt: ...


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

        return None
