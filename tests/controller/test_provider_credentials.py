from uuid import UUID, uuid7

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.provider import (
    create_provider_router,
    handle_invalid_provider_configuration,
)
from inventory_reservation.service.provider_management import (
    InvalidProviderConfigurationError,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderCredentialConfiguration,
    ProviderKind,
    ProviderManagementService,
)


class InMemoryProviderCredentialRepository:
    def __init__(self, provider: ProviderConfiguration) -> None:
        self._provider = provider
        self._credentials: dict[UUID, ProviderCredentialConfiguration] = {}

    async def get(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration | None:
        if provider_id == self._provider.id:
            return self._provider
        return None

    async def upsert_credentials(
        self,
        credentials: ProviderCredentialConfiguration,
    ) -> ProviderCredentialConfiguration:
        self._credentials[credentials.provider_id] = credentials
        return credentials


async def test_operator_can_set_external_provider_credentials() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="credentialed-provider",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://credentialed-provider.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderCredentialRepository(provider)
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/internal/v1/providers/{provider_id}/credentials",
            json={
                "auth_type": "bearer",
                "secret_ref": "vault://inventory/credentialed-provider",
                "public_config": {
                    "header_name": "Authorization",
                    "scheme": "Bearer",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": str(provider_id),
        "auth_type": "bearer",
        "secret_ref": "vault://inventory/credentialed-provider",
        "public_config": {
            "header_name": "Authorization",
            "scheme": "Bearer",
        },
    }


async def test_authenticated_provider_requires_secret_reference() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="missing-secret-reference",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://missing-secret-reference.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderCredentialRepository(provider)
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        InvalidProviderConfigurationError,
        handle_invalid_provider_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/internal/v1/providers/{provider_id}/credentials",
            json={
                "auth_type": "api_key",
                "public_config": {
                    "header_name": "X-API-Key",
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": (
                "Authenticated provider requires a non-blank secret reference."
            ),
        }
    }


async def test_secret_reference_respects_storage_limit() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="long-secret-reference",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://long-secret-reference.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderCredentialRepository(provider)
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        InvalidProviderConfigurationError,
        handle_invalid_provider_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/internal/v1/providers/{provider_id}/credentials",
            json={
                "auth_type": "bearer",
                "secret_ref": "v" * 256,
                "public_config": {},
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": "Secret reference must not exceed 255 characters.",
        }
    }


async def test_unauthenticated_provider_rejects_secret_reference() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="public-provider",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://public-provider.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderCredentialRepository(provider)
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        InvalidProviderConfigurationError,
        handle_invalid_provider_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/internal/v1/providers/{provider_id}/credentials",
            json={
                "auth_type": "none",
                "secret_ref": "vault://unused-secret",
                "public_config": {},
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": (
                "Unauthenticated provider must not define a secret reference."
            ),
        }
    }


async def test_public_credential_config_rejects_secret_values() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="unsafe-public-config",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://unsafe-public-config.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderCredentialRepository(provider)
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        InvalidProviderConfigurationError,
        handle_invalid_provider_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/internal/v1/providers/{provider_id}/credentials",
            json={
                "auth_type": "api_key",
                "secret_ref": "vault://inventory/unsafe-public-config",
                "public_config": {
                    "headers": {
                        "api_key": "raw-secret-must-not-be-stored",
                    }
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": (
                "Public credential config must not contain secret values."
            ),
        }
    }


async def test_internal_provider_rejects_external_credentials() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="internal-credentials",
        kind=ProviderKind.INTERNAL,
        driver="internal",
        base_url=None,
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderCredentialRepository(provider)
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        InvalidProviderConfigurationError,
        handle_invalid_provider_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/internal/v1/providers/{provider_id}/credentials",
            json={
                "auth_type": "bearer",
                "secret_ref": "vault://unused-internal-secret",
                "public_config": {},
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": (
                "Internal provider does not support external credentials."
            ),
        }
    }
