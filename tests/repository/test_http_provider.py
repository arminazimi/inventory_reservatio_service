import json
from uuid import UUID

import httpx
import pytest

from inventory_reservation.repository.provider import (
    EnvironmentSecretResolver,
    HttpInventoryProvider,
    ProviderAuthentication,
)
from inventory_reservation.service.provider import (
    ConfirmCommand,
    HoldCommand,
    ProviderCallFailedError,
    ProviderConfirmAttempt,
    ProviderHoldAttempt,
    ProviderReleaseAttempt,
    ReleaseCommand,
    SecretResolutionError,
)
from inventory_reservation.service.provider_management import (
    ProviderAuthType,
    ProviderCredentialConfiguration,
)

PROVIDER_ID = UUID("00000000-0000-7000-8000-000000000001")
PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000002")


async def test_environment_secret_reference_is_resolved() -> None:
    resolver = EnvironmentSecretResolver(
        environ={"PROVIDER_TOKEN": "resolved-provider-token"}
    )

    secret = await resolver.resolve("env://PROVIDER_TOKEN")

    assert secret == "resolved-provider-token"


class StaticSecretResolver:
    async def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "vault://inventory/provider-token"
        return "resolved-provider-token"


class ApiKeySecretResolver:
    async def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "vault://inventory/provider-api-key"
        return "resolved-api-key"


class FailingSecretResolver:
    async def resolve(self, _: str) -> str:
        raise SecretResolutionError


async def test_bearer_credentials_authenticate_provider_request() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer resolved-provider-token"
        assert (
            request.headers["Idempotency-Key"]
            == "reservation:123:product:456:hold"
        )
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "authenticated-hold"},
        )

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.BEARER,
        secret_ref="vault://inventory/provider-token",
        public_config={
            "header_name": "Authorization",
            "scheme": "Bearer",
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=StaticSecretResolver(),
            ),
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.held(reference="authenticated-hold")


async def test_oauth2_credentials_use_resolved_bearer_token() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer resolved-provider-token"
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "oauth2-hold"},
        )

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.OAUTH2,
        secret_ref="vault://inventory/provider-token",
        public_config={},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=StaticSecretResolver(),
            ),
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.held(reference="oauth2-hold")


async def test_basic_credentials_use_username_and_resolved_password() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert (
            request.headers["Authorization"]
            == "Basic cHJvdmlkZXItdXNlcjpyZXNvbHZlZC1wcm92aWRlci10b2tlbg=="
        )
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "basic-auth-hold"},
        )

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.BASIC,
        secret_ref="vault://inventory/provider-token",
        public_config={"username": "provider-user"},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=StaticSecretResolver(),
            ),
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.held(reference="basic-auth-hold")


async def test_api_key_credentials_use_configured_header() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Provider-Key"] == "resolved-api-key"
        assert (
            request.headers["Idempotency-Key"]
            == "reservation:123:product:456:hold"
        )
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "api-key-hold"},
        )

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.API_KEY,
        secret_ref="vault://inventory/provider-api-key",
        public_config={"header_name": "X-Provider-Key"},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=ApiKeySecretResolver(),
            ),
        )

        attempt = await provider.hold(
            HoldCommand(
                product_id=PRODUCT_ID,
                quantity=2,
                idempotency_key="reservation:123:product:456:hold",
            )
        )

    assert attempt == ProviderHoldAttempt.held(reference="api-key-hold")


async def test_secret_resolution_failure_is_a_provider_call_failure() -> None:
    async def provider_api(_: httpx.Request) -> httpx.Response:
        pytest.fail("Provider must not be called without resolved credentials")

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.BEARER,
        secret_ref="vault://inventory/provider-token",
        public_config={},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=FailingSecretResolver(),
            ),
        )

        with pytest.raises(ProviderCallFailedError) as captured:
            await provider.hold(
                HoldCommand(
                    product_id=PRODUCT_ID,
                    quantity=2,
                    idempotency_key="reservation:123:product:456:hold",
                )
            )

    assert captured.value.provider_id == PROVIDER_ID


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


async def test_server_error_is_reported_as_provider_call_failure() -> None:
    async def provider_api(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_api)) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
        )

        with pytest.raises(ProviderCallFailedError) as captured:
            await provider.hold(
                HoldCommand(
                    product_id=PRODUCT_ID,
                    quantity=2,
                    idempotency_key="reservation:123:product:456:hold",
                )
            )

    assert captured.value.provider_id == PROVIDER_ID


async def test_successful_confirmation_uses_hold_reference_and_idempotency_key() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert (
            request.method,
            request.url.path,
            request.headers["Idempotency-Key"],
            request.content,
        ) == (
            "POST",
            "/holds/external-hold-123/confirm",
            "reservation:123:allocation:456:confirm",
            b"",
        )
        return httpx.Response(status_code=204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_api)) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
        )

        attempt = await provider.confirm(
            ConfirmCommand(
                hold_reference="external-hold-123",
                idempotency_key="reservation:123:allocation:456:confirm",
            )
        )

    assert attempt == ProviderConfirmAttempt.confirmed()


async def test_bearer_credentials_authenticate_confirmation_request() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer resolved-provider-token"
        return httpx.Response(status_code=204)

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.BEARER,
        secret_ref="vault://inventory/provider-token",
        public_config={},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=StaticSecretResolver(),
            ),
        )

        attempt = await provider.confirm(
            ConfirmCommand(
                hold_reference="external-hold-123",
                idempotency_key="reservation:123:allocation:456:confirm",
            )
        )

    assert attempt == ProviderConfirmAttempt.confirmed()


async def test_successful_release_uses_hold_reference_and_idempotency_key() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert (
            request.method,
            request.url.path,
            request.headers["Idempotency-Key"],
            request.content,
        ) == (
            "POST",
            "/holds/external-hold-123/release",
            "reservation:123:allocation:456:release",
            b"",
        )
        return httpx.Response(status_code=204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_api)) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
        )

        attempt = await provider.release(
            ReleaseCommand(
                hold_reference="external-hold-123",
                idempotency_key="reservation:123:allocation:456:release",
            )
        )

    assert attempt == ProviderReleaseAttempt.released()


async def test_bearer_credentials_authenticate_release_request() -> None:
    async def provider_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer resolved-provider-token"
        return httpx.Response(status_code=204)

    credentials = ProviderCredentialConfiguration(
        provider_id=PROVIDER_ID,
        auth_type=ProviderAuthType.BEARER,
        secret_ref="vault://inventory/provider-token",
        public_config={},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_api)
    ) as client:
        provider = HttpInventoryProvider(
            provider_id=PROVIDER_ID,
            base_url="https://inventory-provider.example",
            timeout=2.0,
            client=client,
            authentication=ProviderAuthentication(
                credentials=credentials,
                secret_resolver=StaticSecretResolver(),
            ),
        )

        attempt = await provider.release(
            ReleaseCommand(
                hold_reference="external-hold-123",
                idempotency_key="reservation:123:allocation:456:release",
            )
        )

    assert attempt == ProviderReleaseAttempt.released()
