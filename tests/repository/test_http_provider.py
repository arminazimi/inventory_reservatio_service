import json
from uuid import UUID

import httpx

from inventory_reservation.repository.provider import HttpInventoryProvider
from inventory_reservation.service.provider import HoldCommand, ProviderHoldAttempt

PROVIDER_ID = UUID("00000000-0000-7000-8000-000000000001")
PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000002")


async def test_successful_hold_returns_provider_reference() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert (
            request.method,
            request.url.path,
            request.headers["Idempotency-Key"],
            json.loads(request.content),
        ) == (
            "POST",
            "/holds",
            "reservation:123:product:456:hold",
            {
                "product_id": str(PRODUCT_ID),
                "quantity": 2,
            },
        )
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "external-hold-123"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_api)) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.held(reference="external-hold-123")


async def test_out_of_stock_response_allows_provider_failover() -> None:
    async def provider_api(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=409,
            json={"code": "out_of_stock"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_api)) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.out_of_stock()


async def test_timeout_returns_unknown_outcome() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "Provider response timed out",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_api)) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.unknown()
