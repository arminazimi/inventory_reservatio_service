from uuid import uuid7

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.provider import create_provider_router
from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderManagementService,
)


class InMemoryProviderRepository:
    def __init__(self, providers: tuple[ProviderConfiguration, ...]) -> None:
        self._providers = providers

    async def list(self) -> tuple[ProviderConfiguration, ...]:
        return self._providers


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
