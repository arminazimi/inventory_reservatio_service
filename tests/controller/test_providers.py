from uuid import UUID, uuid7

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.provider import (
    create_provider_router,
    handle_provider_not_found,
)
from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderManagementService,
    ProviderNotFoundError,
)


class InMemoryProviderRepository:
    def __init__(self, providers: tuple[ProviderConfiguration, ...]) -> None:
        self._providers = providers

    async def list(self) -> tuple[ProviderConfiguration, ...]:
        return self._providers

    async def get(self, provider_id: UUID) -> ProviderConfiguration | None:
        return next(
            (
                provider
                for provider in self._providers
                if provider.id == provider_id
            ),
            None,
        )


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
