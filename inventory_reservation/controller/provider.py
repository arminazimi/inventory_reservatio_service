from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Request, status
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

from inventory_reservation.service.provider_management import (
    InvalidProviderConfigurationError,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderManagementService,
    ProviderNameConflictError,
    ProviderNotFoundError,
    RegisterProviderCommand,
)


class ProviderCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability: bool
    hold: bool
    confirm: bool
    release: bool


class RegisterProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ProviderKind
    driver: str
    base_url: str | None = None
    request_timeout_ms: int
    capabilities: ProviderCapabilitiesRequest


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
    provider_id: UUID | None = None
    provider_name: str | None = None


class ProviderErrorResponse(BaseModel):
    error: ProviderErrorDetail


async def handle_invalid_provider_configuration(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, InvalidProviderConfigurationError):
        raise error

    response = ProviderErrorResponse(
        error=ProviderErrorDetail(
            code="invalid_provider_configuration",
            message=str(error),
        )
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=response.model_dump(mode="json", exclude_none=True),
    )


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
        content=response.model_dump(mode="json", exclude_none=True),
    )


async def handle_provider_name_conflict(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, ProviderNameConflictError):
        raise error

    response = ProviderErrorResponse(
        error=ProviderErrorDetail(
            code="provider_name_conflict",
            message="Provider name is already in use.",
            provider_name=error.provider_name,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=response.model_dump(mode="json", exclude_none=True),
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

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_409_CONFLICT: {
                "model": ProviderErrorResponse,
                "description": "Provider name is already registered.",
            },
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ProviderErrorResponse,
                "description": "Provider configuration violates domain rules.",
            }
        },
    )
    async def register_provider(
        request: RegisterProviderRequest,
    ) -> ProviderResponse:
        provider = await provider_management.register_provider(
            RegisterProviderCommand(
                name=request.name,
                kind=request.kind,
                driver=request.driver,
                base_url=request.base_url,
                request_timeout_ms=request.request_timeout_ms,
                capabilities=ProviderCapabilities(
                    availability=request.capabilities.availability,
                    hold=request.capabilities.hold,
                    confirm=request.capabilities.confirm,
                    release=request.capabilities.release,
                ),
            )
        )
        return ProviderResponse.from_domain(provider)

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
