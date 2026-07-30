from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderManagementService,
)


class ProviderCapabilitiesResponse(BaseModel):
    availability: bool
    hold: bool
    confirm: bool
    release: bool

    @classmethod
    def from_domain(
        cls,
        capabilities: ProviderCapabilities,
    ) -> ProviderCapabilitiesResponse:
        return cls(
            availability=capabilities.availability,
            hold=capabilities.hold,
            confirm=capabilities.confirm,
            release=capabilities.release,
        )


class ProviderResponse(BaseModel):
    id: UUID
    name: str
    kind: ProviderKind
    driver: str
    base_url: str | None
    request_timeout_ms: int
    capabilities: ProviderCapabilitiesResponse
    is_enabled: bool

    @classmethod
    def from_domain(cls, provider: ProviderConfiguration) -> ProviderResponse:
        return cls(
            id=provider.id,
            name=provider.name,
            kind=provider.kind,
            driver=provider.driver,
            base_url=provider.base_url,
            request_timeout_ms=provider.request_timeout_ms,
            capabilities=ProviderCapabilitiesResponse.from_domain(
                provider.capabilities
            ),
            is_enabled=provider.is_enabled,
        )


def create_provider_router(
    provider_management: ProviderManagementService,
) -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1/providers",
        tags=["internal providers"],
    )

    @router.get("")
    async def list_providers() -> list[ProviderResponse]:
        providers = await provider_management.list_providers()
        return [ProviderResponse.from_domain(provider) for provider in providers]

    return router
