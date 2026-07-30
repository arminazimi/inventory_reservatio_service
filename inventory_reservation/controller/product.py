from uuid import UUID

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

from inventory_reservation.service.product import (
    CreateProductCommand,
    InvalidProductConfigurationError,
    Product,
    ProductManagementService,
    ProductNotFoundError,
    ProductSkuConflictError,
)


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    name: str


class ProductResponse(BaseModel):
    id: UUID
    sku: str
    name: str
    is_active: bool

    @classmethod
    def from_domain(cls, product: Product) -> ProductResponse:
        return cls(
            id=product.id,
            sku=product.sku,
            name=product.name,
            is_active=product.is_active,
        )


class ProductErrorDetail(BaseModel):
    code: str
    message: str
    sku: str | None = None
    product_id: UUID | None = None


class ProductErrorResponse(BaseModel):
    error: ProductErrorDetail


async def handle_invalid_product_configuration(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, InvalidProductConfigurationError):
        raise error

    response = ProductErrorResponse(
        error=ProductErrorDetail(
            code="invalid_product_configuration",
            message=str(error),
        )
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=response.model_dump(mode="json", exclude_none=True),
    )


async def handle_product_sku_conflict(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, ProductSkuConflictError):
        raise error

    response = ProductErrorResponse(
        error=ProductErrorDetail(
            code="product_sku_conflict",
            message="Product SKU is already in use.",
            sku=error.sku,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=response.model_dump(mode="json", exclude_none=True),
    )


async def handle_product_not_found(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, ProductNotFoundError):
        raise error

    response = ProductErrorResponse(
        error=ProductErrorDetail(
            code="product_not_found",
            message="Product was not found.",
            product_id=error.product_id,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=response.model_dump(mode="json", exclude_none=True),
    )


def create_product_router(
    product_management: ProductManagementService,
) -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1/products",
        tags=["internal products"],
    )

    @router.get("")
    async def list_products() -> list[ProductResponse]:
        products = await product_management.list_products()
        return [
            ProductResponse.from_domain(product)
            for product in products
        ]

    @router.get(
        "/{product_id}",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ProductErrorResponse,
                "description": "Product was not found.",
            }
        },
    )
    async def get_product(product_id: UUID) -> ProductResponse:
        product = await product_management.get_product(product_id)
        return ProductResponse.from_domain(product)

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_409_CONFLICT: {
                "model": ProductErrorResponse,
                "description": "Product SKU is already registered.",
            },
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ProductErrorResponse,
                "description": "Product configuration violates domain rules.",
            }
        },
    )
    async def create_product(
        request: CreateProductRequest,
    ) -> ProductResponse:
        product = await product_management.create_product(
            CreateProductCommand(
                sku=request.sku,
                name=request.name,
            )
        )
        return ProductResponse.from_domain(product)

    return router
