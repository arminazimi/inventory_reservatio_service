from uuid import UUID

import pytest

from inventory_reservation.service.provider import (
    HoldCommand,
    ProviderHold,
    ProviderHoldAttempt,
    ProviderRouter,
    UnknownProviderOutcomeError,
)

FIRST_PROVIDER_ID = UUID("00000000-0000-7000-8000-000000000001")
SECOND_PROVIDER_ID = UUID("00000000-0000-7000-8000-000000000002")
PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000003")


class StubHoldProvider:
    def __init__(
        self,
        *,
        provider_id: UUID,
        attempt: ProviderHoldAttempt,
    ) -> None:
        self.provider_id = provider_id
        self._attempt = attempt

    async def hold(self, _: HoldCommand) -> ProviderHoldAttempt:
        return self._attempt


async def test_hold_falls_back_when_preferred_provider_is_out_of_stock() -> None:
    preferred_provider = StubHoldProvider(
        provider_id=FIRST_PROVIDER_ID,
        attempt=ProviderHoldAttempt.out_of_stock(),
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="fallback-hold-123"),
    )
    router = ProviderRouter((preferred_provider, fallback_provider))

    hold = await router.hold(
        HoldCommand(
            product_id=PRODUCT_ID,
            quantity=2,
            idempotency_key="reservation:123:product:456:hold",
        )
    )

    assert hold == ProviderHold(
        provider_id=SECOND_PROVIDER_ID,
        reference="fallback-hold-123",
    )


async def test_hold_does_not_fall_back_when_provider_outcome_is_unknown() -> None:
    unknown_provider = StubHoldProvider(
        provider_id=FIRST_PROVIDER_ID,
        attempt=ProviderHoldAttempt.unknown(),
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="unsafe-fallback-hold"),
    )
    router = ProviderRouter((unknown_provider, fallback_provider))
    command = HoldCommand(
        product_id=PRODUCT_ID,
        quantity=2,
        idempotency_key="reservation:789:product:456:hold",
    )

    with pytest.raises(UnknownProviderOutcomeError) as captured:
        await router.hold(command)

    assert (
        captured.value.provider_id,
        captured.value.idempotency_key,
    ) == (
        FIRST_PROVIDER_ID,
        "reservation:789:product:456:hold",
    )
