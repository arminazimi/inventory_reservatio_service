import os
from uuid import UUID

import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import ProductModel
from inventory_reservation.repository.product import SqlAlchemyProductRepository
from inventory_reservation.service.product import (
    Product,
    ProductSkuConflictError,
)

PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000011")
DUPLICATE_PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000012")


@pytest.mark.integration
async def test_repository_makes_created_product_retrievable() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    repository = SqlAlchemyProductRepository(database)
    product = Product(
        id=PRODUCT_ID,
        sku="REPOSITORY-PRODUCT-100",
        name="Repository product",
        is_active=True,
    )

    try:
        await repository.add(product)

        retrieved = await repository.get(PRODUCT_ID)

        assert retrieved == product
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(ProductModel).where(ProductModel.id == PRODUCT_ID)
            )
        await database.close()


@pytest.mark.integration
async def test_repository_maps_duplicate_sku_to_domain_conflict() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    repository = SqlAlchemyProductRepository(database)
    original = Product(
        id=PRODUCT_ID,
        sku="DUPLICATE-REPOSITORY-SKU",
        name="Original product",
        is_active=True,
    )
    duplicate = Product(
        id=DUPLICATE_PRODUCT_ID,
        sku="DUPLICATE-REPOSITORY-SKU",
        name="Duplicate product",
        is_active=True,
    )

    try:
        await repository.add(original)

        with pytest.raises(ProductSkuConflictError) as captured:
            await repository.add(duplicate)

        assert captured.value.sku == "DUPLICATE-REPOSITORY-SKU"
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(ProductModel).where(
                    ProductModel.id.in_(
                        (PRODUCT_ID, DUPLICATE_PRODUCT_ID)
                    )
                )
            )
        await database.close()
