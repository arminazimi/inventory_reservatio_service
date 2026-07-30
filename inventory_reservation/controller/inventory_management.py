from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from inventory_reservation.service.inventory_management import (
    InventoryBelowReservedError,
    InventoryLevel,
    InventoryLevelNotFoundError,
    InventoryManagementService,
    SetInventoryLevelCommand,
)


class SetInventoryLevelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_hand: int = Field(ge=0)
    allocation_priority: int = Field(ge=0)


class InventoryLevelResponse(BaseModel):
    product_id: UUID
    provider_id: UUID
    on_hand: int
    reserved: int
    available: int
    allocation_priority: int
    version: int

    @classmethod
    def from_domain(
        cls,
        level: InventoryLevel,
    ) -> InventoryLevelResponse:
        return cls(
            product_id=level.product_id,
            provider_id=level.provider_id,
            on_hand=level.on_hand,
            reserved=level.reserved,
            available=level.available,
            allocation_priority=level.allocation_priority,
            version=level.version,
        )


class InventoryErrorDetail(BaseModel):
    code: str
    message: str
    product_id: UUID
    provider_id: UUID
    reserved: int | None = None


class InventoryErrorResponse(BaseModel):
    error: InventoryErrorDetail


async def handle_inventory_below_reserved(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, InventoryBelowReservedError):
        raise error

    response = InventoryErrorResponse(
        error=InventoryErrorDetail(
            code="inventory_below_reserved",
            message=str(error),
            product_id=error.product_id,
            provider_id=error.provider_id,
            reserved=error.reserved,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=response.model_dump(mode="json", exclude_none=True),
    )


async def handle_inventory_level_not_found(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, InventoryLevelNotFoundError):
        raise error

    response = InventoryErrorResponse(
        error=InventoryErrorDetail(
            code="inventory_level_not_found",
            message="Inventory level was not found.",
            product_id=error.product_id,
            provider_id=error.provider_id,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=response.model_dump(mode="json", exclude_none=True),
    )


def create_inventory_management_router(
    inventory_management: InventoryManagementService,
) -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1/products",
        tags=["internal inventory"],
    )

    @router.get("/{product_id}/inventory")
    async def list_product_inventory(
        product_id: Annotated[UUID, Path()],
    ) -> list[InventoryLevelResponse]:
        levels = await inventory_management.list_product_inventory(
            product_id
        )
        return [
            InventoryLevelResponse.from_domain(level)
            for level in levels
        ]

    @router.get(
        "/{product_id}/providers/{provider_id}/inventory",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": InventoryErrorResponse,
                "description": "Inventory assignment was not found.",
            }
        },
    )
    async def get_inventory_level(
        product_id: Annotated[UUID, Path()],
        provider_id: Annotated[UUID, Path()],
    ) -> InventoryLevelResponse:
        level = await inventory_management.get_inventory_level(
            product_id=product_id,
            provider_id=provider_id,
        )
        return InventoryLevelResponse.from_domain(level)

    @router.put(
        "/{product_id}/providers/{provider_id}/inventory",
        responses={
            status.HTTP_409_CONFLICT: {
                "model": InventoryErrorResponse,
                "description": (
                    "On-hand inventory is below reserved inventory."
                ),
            }
        },
    )
    async def set_inventory_level(
        product_id: Annotated[UUID, Path()],
        provider_id: Annotated[UUID, Path()],
        request: SetInventoryLevelRequest,
    ) -> InventoryLevelResponse:
        level = await inventory_management.set_inventory_level(
            SetInventoryLevelCommand(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=request.on_hand,
                allocation_priority=request.allocation_priority,
            )
        )
        return InventoryLevelResponse.from_domain(level)

    return router
