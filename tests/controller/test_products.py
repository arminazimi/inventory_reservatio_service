from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.product import (
    create_product_router,
    handle_invalid_product_configuration,
    handle_product_sku_conflict,
)
from inventory_reservation.service.product import (
    InvalidProductConfigurationError,
    Product,
    ProductManagementService,
    ProductSkuConflictError,
)

PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000010")


class InMemoryProductRepository:
    def __init__(self) -> None:
        self.products: list[Product] = []

    async def add(self, product: Product) -> None:
        if any(
            existing.sku == product.sku
            for existing in self.products
        ):
            raise ProductSkuConflictError(product.sku)
        self.products.append(product)

    async def get(self, product_id: UUID) -> Product | None:
        return next(
            (
                product
                for product in self.products
                if product.id == product_id
            ),
            None,
        )


async def test_operator_can_create_product_projection() -> None:
    repository = InMemoryProductRepository()
    service = ProductManagementService(
        repository=repository,
        product_id_factory=lambda: PRODUCT_ID,
    )
    app = FastAPI()
    app.include_router(create_product_router(service))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "HEADPHONES-100",
                "name": "Noise-cancelling headphones",
            },
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": str(PRODUCT_ID),
        "sku": "HEADPHONES-100",
        "name": "Noise-cancelling headphones",
        "is_active": True,
    }


async def test_product_sku_must_not_be_blank() -> None:
    service = ProductManagementService(
        repository=InMemoryProductRepository(),
        product_id_factory=lambda: PRODUCT_ID,
    )
    app = FastAPI()
    app.include_router(create_product_router(service))
    app.add_exception_handler(
        InvalidProductConfigurationError,
        handle_invalid_product_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "   ",
                "name": "Noise-cancelling headphones",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_product_configuration",
            "message": "Product SKU must not be blank.",
        }
    }


async def test_product_name_must_not_be_blank() -> None:
    service = ProductManagementService(
        repository=InMemoryProductRepository(),
        product_id_factory=lambda: PRODUCT_ID,
    )
    app = FastAPI()
    app.include_router(create_product_router(service))
    app.add_exception_handler(
        InvalidProductConfigurationError,
        handle_invalid_product_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "HEADPHONES-100",
                "name": "   ",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_product_configuration",
            "message": "Product name must not be blank.",
        }
    }


async def test_product_sku_respects_storage_limit() -> None:
    service = ProductManagementService(
        repository=InMemoryProductRepository(),
        product_id_factory=lambda: PRODUCT_ID,
    )
    app = FastAPI()
    app.include_router(create_product_router(service))
    app.add_exception_handler(
        InvalidProductConfigurationError,
        handle_invalid_product_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "S" * 101,
                "name": "Noise-cancelling headphones",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_product_configuration",
            "message": "Product SKU must not exceed 100 characters.",
        }
    }


async def test_product_name_respects_storage_limit() -> None:
    service = ProductManagementService(
        repository=InMemoryProductRepository(),
        product_id_factory=lambda: PRODUCT_ID,
    )
    app = FastAPI()
    app.include_router(create_product_router(service))
    app.add_exception_handler(
        InvalidProductConfigurationError,
        handle_invalid_product_configuration,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "HEADPHONES-100",
                "name": "N" * 256,
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_product_configuration",
            "message": "Product name must not exceed 255 characters.",
        }
    }


async def test_duplicate_product_sku_returns_conflict() -> None:
    repository = InMemoryProductRepository()
    service = ProductManagementService(
        repository=repository,
        product_id_factory=lambda: PRODUCT_ID,
    )
    app = FastAPI()
    app.include_router(create_product_router(service))
    app.add_exception_handler(
        ProductSkuConflictError,
        handle_product_sku_conflict,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "HEADPHONES-100",
                "name": "Noise-cancelling headphones",
            },
        )
        duplicate_response = await client.post(
            "/internal/v1/products",
            json={
                "sku": "HEADPHONES-100",
                "name": "Duplicate headphones",
            },
        )

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json() == {
        "error": {
            "code": "product_sku_conflict",
            "message": "Product SKU is already in use.",
            "sku": "HEADPHONES-100",
        }
    }
