from uuid import UUID, uuid7

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.provider import (
    create_provider_router,
    handle_invalid_provider_configuration,
    handle_provider_name_conflict,
    handle_provider_not_found,
)
from inventory_reservation.service.provider_management import (
    InvalidProviderConfigurationError,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderManagementService,
    ProviderNameConflictError,
    ProviderNotFoundError,
)


class InMemoryProviderRepository:
    def __init__(self, providers: tuple[ProviderConfiguration, ...]) -> None:
        self._providers = list(providers)

    async def list(self) -> tuple[ProviderConfiguration, ...]:
        return tuple(self._providers)

    async def get(self, provider_id: UUID) -> ProviderConfiguration | None:
        return next(
            (
                provider
                for provider in self._providers
                if provider.id == provider_id
            ),
            None,
        )

    async def add(self, provider: ProviderConfiguration) -> None:
        if any(
            existing.name == provider.name
            for existing in self._providers
        ):
            raise ProviderNameConflictError(provider.name)
        self._providers.append(provider)


async def test_operator_can_list_provider_configurations() -> None:
    provider_id = uuid7()
    repository = InMemoryProviderRepository(
        (
            ProviderConfiguration(
                id=provider_id,
                name="marketplace-one",
                kind=ProviderKind.EXTERNAL,
                driver="http",
                base_url="https://inventory.provider.test",
                request_timeout_ms=1_500,
                capabilities=ProviderCapabilities(
                    availability=True,
                    hold=True,
                    confirm=True,
                    release=True,
                ),
                is_enabled=True,
            ),
        )
    )
    service = ProviderManagementService(repository=repository)

    app = FastAPI()
    app.include_router(create_provider_router(service))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/internal/v1/providers")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(provider_id),
            "name": "marketplace-one",
            "kind": "external",
            "driver": "http",
            "base_url": "https://inventory.provider.test",
            "request_timeout_ms": 1_500,
            "capabilities": {
                "availability": True,
                "hold": True,
                "confirm": True,
                "release": True,
            },
            "is_enabled": True,
        }
    ]


async def test_operator_can_get_provider_configuration() -> None:
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name="internal-warehouse",
        kind=ProviderKind.INTERNAL,
        driver="internal",
        base_url=None,
        request_timeout_ms=2_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    service = ProviderManagementService(
        repository=InMemoryProviderRepository((provider,))
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/internal/v1/providers/{provider_id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(provider_id),
        "name": "internal-warehouse",
        "kind": "internal",
        "driver": "internal",
        "base_url": None,
        "request_timeout_ms": 2_000,
        "capabilities": {
            "availability": True,
            "hold": True,
            "confirm": True,
            "release": True,
        },
        "is_enabled": False,
    }


async def test_get_provider_returns_not_found_for_unknown_provider() -> None:
    provider_id = uuid7()
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        ProviderNotFoundError,
        handle_provider_not_found,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/internal/v1/providers/{provider_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "provider_not_found",
            "message": "Provider was not found.",
            "provider_id": str(provider_id),
        }
    }


async def test_operator_can_register_external_provider() -> None:
    provider_id = uuid7()
    repository = InMemoryProviderRepository(())
    service = ProviderManagementService(
        repository=repository,
        provider_id_factory=lambda: provider_id,
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "marketplace-two",
                "kind": "external",
                "driver": "http",
                "base_url": "https://inventory.marketplace-two.test",
                "request_timeout_ms": 1_250,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": str(provider_id),
        "name": "marketplace-two",
        "kind": "external",
        "driver": "http",
        "base_url": "https://inventory.marketplace-two.test",
        "request_timeout_ms": 1_250,
        "capabilities": {
            "availability": True,
            "hold": True,
            "confirm": True,
            "release": True,
        },
        "is_enabled": False,
    }


async def test_register_external_provider_requires_base_url() -> None:
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
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
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "missing-url",
                "kind": "external",
                "driver": "http",
                "request_timeout_ms": 1_000,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": "External provider requires a base URL.",
        }
    }


async def test_register_hold_provider_requires_full_reservation_lifecycle() -> None:
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
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
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "hold-only-provider",
                "kind": "external",
                "driver": "http",
                "base_url": "https://hold-only.test",
                "request_timeout_ms": 1_000,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": False,
                    "release": True,
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": (
                "Hold-capable provider must support confirm and release."
            ),
        }
    }


async def test_register_provider_rejects_duplicate_name() -> None:
    existing_provider = ProviderConfiguration(
        id=uuid7(),
        name="duplicate-provider",
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
        repository=InMemoryProviderRepository((existing_provider,))
    )
    app = FastAPI()
    app.include_router(create_provider_router(service))
    app.add_exception_handler(
        ProviderNameConflictError,
        handle_provider_name_conflict,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "duplicate-provider",
                "kind": "internal",
                "driver": "internal",
                "request_timeout_ms": 1_000,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "provider_name_conflict",
            "message": "Provider name is already in use.",
            "provider_name": "duplicate-provider",
        }
    }


async def test_register_provider_requires_positive_timeout() -> None:
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
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
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "zero-timeout-provider",
                "kind": "internal",
                "driver": "internal",
                "request_timeout_ms": 0,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": "Provider request timeout must be positive.",
        }
    }


async def test_register_provider_rejects_blank_name() -> None:
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
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
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "   ",
                "kind": "internal",
                "driver": "internal",
                "request_timeout_ms": 1_000,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": "Provider name must not be blank.",
        }
    }


async def test_register_external_provider_rejects_unsupported_driver() -> None:
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
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
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "grpc-provider",
                "kind": "external",
                "driver": "grpc",
                "base_url": "https://grpc-provider.test",
                "request_timeout_ms": 1_000,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": "External provider driver must be 'http'.",
        }
    }


async def test_register_external_provider_requires_http_base_url() -> None:
    service = ProviderManagementService(
        repository=InMemoryProviderRepository(())
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
        response = await client.post(
            "/internal/v1/providers",
            json={
                "name": "ftp-provider",
                "kind": "external",
                "driver": "http",
                "base_url": "ftp://inventory.provider.test",
                "request_timeout_ms": 1_000,
                "capabilities": {
                    "availability": True,
                    "hold": True,
                    "confirm": True,
                    "release": True,
                },
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_provider_configuration",
            "message": "External provider base URL must use HTTP or HTTPS.",
        }
    }
