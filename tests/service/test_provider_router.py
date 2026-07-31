import asyncio
from uuid import UUID

import pytest

from inventory_reservation.service.provider import (
    AvailabilityCommand,
    CircuitBreakerProvider,
    HoldCommand,
    ProviderAvailabilityAttempt,
    ProviderCallFailedError,
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


class AvailabilityAwareStubProvider(StubHoldProvider):
    def __init__(
        self,
        *,
        provider_id: UUID,
        availability: ProviderAvailabilityAttempt,
        hold: ProviderHoldAttempt,
    ) -> None:
        super().__init__(provider_id=provider_id, attempt=hold)
        self._availability = availability
        self.hold_calls = 0

    async def availability(
        self,
        _: AvailabilityCommand,
    ) -> ProviderAvailabilityAttempt:
        return self._availability

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt:
        self.hold_calls += 1
        return await super().hold(command)


class HealthyAvailabilityFailingHoldProvider:
    def __init__(self, *, provider_id: UUID) -> None:
        self.provider_id = provider_id
        self.hold_calls = 0

    async def availability(
        self,
        _: AvailabilityCommand,
    ) -> ProviderAvailabilityAttempt:
        return ProviderAvailabilityAttempt.fresh(available_quantity=10)

    async def hold(self, _: HoldCommand) -> ProviderHoldAttempt:
        self.hold_calls += 1
        raise ProviderCallFailedError(self.provider_id)


class RecoveringHoldProvider:
    def __init__(self, *, provider_id: UUID) -> None:
        self.provider_id = provider_id
        self._remaining_failures = 2

    async def hold(self, _: HoldCommand) -> ProviderHoldAttempt:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise ProviderCallFailedError(self.provider_id)
        return ProviderHoldAttempt.held(reference="recovered-primary-hold")


class RecoveringUnknownProvider:
    def __init__(self, *, provider_id: UUID) -> None:
        self.provider_id = provider_id
        self._remaining_unknown_outcomes = 2

    async def hold(self, _: HoldCommand) -> ProviderHoldAttempt:
        if self._remaining_unknown_outcomes > 0:
            self._remaining_unknown_outcomes -= 1
            return ProviderHoldAttempt.unknown()
        return ProviderHoldAttempt.held(reference="unsafe-primary-hold")


class BlockingRecoveryProvider:
    def __init__(self, *, provider_id: UUID) -> None:
        self.provider_id = provider_id
        self._remaining_failures = 2
        self.probe_started = asyncio.Event()
        self.release_probe = asyncio.Event()

    async def hold(self, _: HoldCommand) -> ProviderHoldAttempt:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise ProviderCallFailedError(self.provider_id)

        self.probe_started.set()
        await self.release_probe.wait()
        return ProviderHoldAttempt.held(reference="half-open-probe-hold")


class FakeMonotonicClock:
    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


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


async def test_fresh_insufficient_availability_skips_to_next_provider() -> None:
    insufficient_provider = AvailabilityAwareStubProvider(
        provider_id=FIRST_PROVIDER_ID,
        availability=ProviderAvailabilityAttempt.fresh(available_quantity=1),
        hold=ProviderHoldAttempt.held(reference="must-not-be-used"),
    )
    fallback_provider = AvailabilityAwareStubProvider(
        provider_id=SECOND_PROVIDER_ID,
        availability=ProviderAvailabilityAttempt.fresh(available_quantity=5),
        hold=ProviderHoldAttempt.held(reference="fallback-after-availability"),
    )

    hold = await ProviderRouter((insufficient_provider, fallback_provider)).hold(
        HoldCommand(
            product_id=PRODUCT_ID,
            quantity=2,
            idempotency_key="availability-aware-hold",
        )
    )

    assert hold == ProviderHold(
        provider_id=SECOND_PROVIDER_ID,
        reference="fallback-after-availability",
    )
    assert (insufficient_provider.hold_calls, fallback_provider.hold_calls) == (0, 1)


async def test_stale_availability_is_not_trusted_over_atomic_hold() -> None:
    stale_provider = AvailabilityAwareStubProvider(
        provider_id=FIRST_PROVIDER_ID,
        availability=ProviderAvailabilityAttempt.stale(available_quantity=0),
        hold=ProviderHoldAttempt.held(reference="authoritative-hold"),
    )

    hold = await ProviderRouter((stale_provider,)).hold(
        HoldCommand(
            product_id=PRODUCT_ID,
            quantity=2,
            idempotency_key="stale-availability-hold",
        )
    )

    assert hold == ProviderHold(
        provider_id=FIRST_PROVIDER_ID,
        reference="authoritative-hold",
    )
    assert stale_provider.hold_calls == 1


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


async def test_open_circuit_routes_subsequent_holds_to_fallback_provider() -> None:
    protected_provider = CircuitBreakerProvider(
        provider=RecoveringHoldProvider(provider_id=FIRST_PROVIDER_ID),
        failure_threshold=2,
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="fallback-hold"),
    )
    router = ProviderRouter((protected_provider, fallback_provider))

    for attempt_number in (1, 2):
        await router.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key=f"reservation:{attempt_number}:product:456:hold",
            )
        )

    hold_after_circuit_opens = await router.hold(
        HoldCommand(
            product_id=PRODUCT_ID,
            quantity=2,
            idempotency_key="reservation:3:product:456:hold",
        )
    )

    assert hold_after_circuit_opens == ProviderHold(
        provider_id=SECOND_PROVIDER_ID,
        reference="fallback-hold",
    )


async def test_successful_availability_does_not_reset_hold_circuit() -> None:
    primary = HealthyAvailabilityFailingHoldProvider(provider_id=FIRST_PROVIDER_ID)
    protected_provider = CircuitBreakerProvider(
        provider=primary,
        failure_threshold=2,
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="fallback-hold"),
    )
    router = ProviderRouter((protected_provider, fallback_provider))

    for attempt_number in (1, 2, 3):
        hold = await router.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key=f"availability-then-hold:{attempt_number}",
            )
        )
        assert hold is not None
        assert hold.provider_id == SECOND_PROVIDER_ID

    assert primary.hold_calls == 2


async def test_open_circuit_probes_provider_after_recovery_timeout() -> None:
    clock = FakeMonotonicClock()
    protected_provider = CircuitBreakerProvider(
        provider=RecoveringHoldProvider(provider_id=FIRST_PROVIDER_ID),
        failure_threshold=2,
        recovery_timeout=30.0,
        monotonic=clock,
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="fallback-hold"),
    )
    router = ProviderRouter((protected_provider, fallback_provider))

    for attempt_number in (1, 2):
        await router.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key=f"reservation:{attempt_number}:product:456:hold",
            )
        )

    clock.advance(30.0)
    recovered_hold = await router.hold(
        HoldCommand(
            product_id=PRODUCT_ID,
            quantity=2,
            idempotency_key="reservation:3:product:456:hold",
        )
    )

    assert recovered_hold == ProviderHold(
        provider_id=FIRST_PROVIDER_ID,
        reference="recovered-primary-hold",
    )


async def test_repeated_unknown_outcomes_open_circuit_for_subsequent_holds() -> None:
    protected_provider = CircuitBreakerProvider(
        provider=RecoveringUnknownProvider(provider_id=FIRST_PROVIDER_ID),
        failure_threshold=2,
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="safe-fallback-hold"),
    )
    router = ProviderRouter((protected_provider, fallback_provider))

    for attempt_number in (1, 2):
        with pytest.raises(UnknownProviderOutcomeError):
            await router.hold(
                HoldCommand(
                    product_id=PRODUCT_ID,
                    quantity=2,
                    idempotency_key=f"reservation:{attempt_number}:product:456:hold",
                )
            )

    hold_after_circuit_opens = await router.hold(
        HoldCommand(
            product_id=PRODUCT_ID,
            quantity=2,
            idempotency_key="reservation:3:product:456:hold",
        )
    )

    assert hold_after_circuit_opens == ProviderHold(
        provider_id=SECOND_PROVIDER_ID,
        reference="safe-fallback-hold",
    )


async def test_half_open_circuit_allows_only_one_concurrent_probe() -> None:
    clock = FakeMonotonicClock()
    recovering_provider = BlockingRecoveryProvider(provider_id=FIRST_PROVIDER_ID)
    protected_provider = CircuitBreakerProvider(
        provider=recovering_provider,
        failure_threshold=2,
        recovery_timeout=30.0,
        monotonic=clock,
    )
    fallback_provider = StubHoldProvider(
        provider_id=SECOND_PROVIDER_ID,
        attempt=ProviderHoldAttempt.held(reference="fallback-hold"),
    )
    router = ProviderRouter((protected_provider, fallback_provider))

    for attempt_number in (1, 2):
        await router.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key=f"reservation:{attempt_number}:product:456:hold",
            )
        )

    clock.advance(30.0)
    first_hold_task = asyncio.create_task(
        router.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:3:product:456:hold",
            )
        )
    )
    await recovering_provider.probe_started.wait()
    second_hold_task = asyncio.create_task(
        router.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:4:product:456:hold",
            )
        )
    )
    await asyncio.sleep(0)
    recovering_provider.release_probe.set()

    holds = await asyncio.gather(first_hold_task, second_hold_task)

    assert set(holds) == {
        ProviderHold(
            provider_id=FIRST_PROVIDER_ID,
            reference="half-open-probe-hold",
        ),
        ProviderHold(
            provider_id=SECOND_PROVIDER_ID,
            reference="fallback-hold",
        ),
    }
