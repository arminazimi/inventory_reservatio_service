from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Request, status
from pydantic import BaseModel
from starlette.responses import JSONResponse

from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderManagementService,
    ProviderNotFoundError,
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


class ProviderErrorDetail(BaseModel):
    code: str
    message: str
    provider_id: UUID


class ProviderErrorResponse(BaseModel):
    error: ProviderErrorDetail


async def handle_provider_not_found(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, ProviderNotFoundError):
        raise error

    response = ProviderErrorResponse(
        error=ProviderErrorDetail(
            code="provider_not_found",
            message="Provider was not found.",
            provider_id=error.provider_id,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=response.model_dump(mode="json"),
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

    @router.get(
        "/{provider_id}",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ProviderErrorResponse,
                "description": "Provider was not found.",
            }
        },
    )
    async def get_provider(
        provider_id: Annotated[UUID, Path()],
    ) -> ProviderResponse:
        provider = await provider_management.get_provider(provider_id)
        return ProviderResponse.from_domain(provider)

    return router
