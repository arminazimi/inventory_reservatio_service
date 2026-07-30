from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from inventory_reservation.service.provider_management import (
    InvalidProviderConfigurationError,
    ProviderAuthType,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderCredentialConfiguration,
    ProviderKind,
    ProviderManagementService,
    ProviderNameConflictError,
    ProviderNotFoundError,
    RegisterProviderCommand,
    SetProviderCredentialsCommand,
    UpdateProviderCommand,
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


class UpdateProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    base_url: str | None = None
    request_timeout_ms: int | None = None
    capabilities: ProviderCapabilitiesRequest | None = None


class SetProviderCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_type: ProviderAuthType
    secret_ref: str | None = None
    public_config: dict[str, object] = Field(default_factory=dict)


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


class ProviderCredentialsResponse(BaseModel):
    provider_id: UUID
    auth_type: ProviderAuthType
    secret_ref: str | None
    public_config: dict[str, object]

    @classmethod
    def from_domain(
        cls,
        credentials: ProviderCredentialConfiguration,
    ) -> ProviderCredentialsResponse:
        return cls(
            provider_id=credentials.provider_id,
            auth_type=credentials.auth_type,
            secret_ref=credentials.secret_ref,
            public_config=credentials.public_config,
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

    @router.patch(
        "/{provider_id}",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ProviderErrorResponse,
                "description": "Provider was not found.",
            },
            status.HTTP_409_CONFLICT: {
                "model": ProviderErrorResponse,
                "description": "Provider name is already registered.",
            },
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ProviderErrorResponse,
                "description": "Provider configuration violates domain rules.",
            },
        },
    )
    async def update_provider(
        provider_id: Annotated[UUID, Path()],
        request: UpdateProviderRequest,
    ) -> ProviderResponse:
        capabilities = (
            ProviderCapabilities(
                availability=request.capabilities.availability,
                hold=request.capabilities.hold,
                confirm=request.capabilities.confirm,
                release=request.capabilities.release,
            )
            if request.capabilities is not None
            else None
        )
        provider = await provider_management.update_provider(
            provider_id,
            UpdateProviderCommand(
                name=request.name,
                base_url=request.base_url,
                request_timeout_ms=request.request_timeout_ms,
                capabilities=capabilities,
            ),
        )
        return ProviderResponse.from_domain(provider)

    @router.post(
        "/{provider_id}/enable",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ProviderErrorResponse,
                "description": "Provider was not found.",
            },
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ProviderErrorResponse,
                "description": "Provider configuration violates domain rules.",
            },
        },
    )
    async def enable_provider(
        provider_id: Annotated[UUID, Path()],
    ) -> ProviderResponse:
        provider = await provider_management.enable_provider(provider_id)
        return ProviderResponse.from_domain(provider)

    @router.post(
        "/{provider_id}/disable",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ProviderErrorResponse,
                "description": "Provider was not found.",
            }
        },
    )
    async def disable_provider(
        provider_id: Annotated[UUID, Path()],
    ) -> ProviderResponse:
        provider = await provider_management.disable_provider(provider_id)
        return ProviderResponse.from_domain(provider)

    @router.put(
        "/{provider_id}/credentials",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ProviderErrorResponse,
                "description": "Provider was not found.",
            },
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ProviderErrorResponse,
                "description": "Credential configuration violates domain rules.",
            },
        },
    )
    async def set_provider_credentials(
        provider_id: Annotated[UUID, Path()],
        request: SetProviderCredentialsRequest,
    ) -> ProviderCredentialsResponse:
        credentials = await provider_management.set_provider_credentials(
            provider_id,
            SetProviderCredentialsCommand(
                auth_type=request.auth_type,
                secret_ref=request.secret_ref,
                public_config=request.public_config,
            ),
        )
        return ProviderCredentialsResponse.from_domain(credentials)

    return router
