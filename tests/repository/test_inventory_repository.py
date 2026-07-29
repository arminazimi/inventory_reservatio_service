import os

import pytest

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory import (
    InventoryRepository,
    InventorySnapshot,
)
from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProductModel,
    ProviderKind,
)


@pytest.mark.integration
async def test_try_hold_reserves_available_stock() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        async with database.session() as session:
            try:
                product = ProductModel(sku="HOLD-TEST", name="Hold test product")
                provider = InventoryProviderModel(
                    name="internal-hold-test",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                    supports_confirm=True,
                    supports_release=True,
                )
                session.add_all([product, provider])
                await session.flush()

                level = InventoryLevelModel(
                    product_id=product.id,
                    provider_id=provider.id,
                    on_hand=5,
                    reserved=0,
                )
                session.add(level)
                await session.flush()

                snapshot = await InventoryRepository(session).try_hold(
                    product_id=product.id,
                    provider_id=provider.id,
                    quantity=2,
                )

                assert snapshot == InventorySnapshot(
                    product_id=product.id,
                    provider_id=provider.id,
                    on_hand=5,
                    reserved=2,
                    version=2,
                )
            finally:
                await session.rollback()
    finally:
        await database.close()
