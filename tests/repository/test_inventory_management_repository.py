import os
from uuid import uuid7

import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory_management import (
    SqlAlchemyInventoryLevelRepository,
)
from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProductModel,
    ProviderKind,
)
from inventory_reservation.service.inventory_management import (
    InventoryBelowReservedError,
    InventoryLevel,
)


@pytest.mark.integration
async def test_inventory_assignment_is_persisted_idempotently() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    unique_suffix = uuid7().hex
    product_id = uuid7()
    provider_id = uuid7()

    try:
        async with database.session() as session, session.begin():
            product = ProductModel(
                id=product_id,
                sku=f"INVENTORY-MANAGEMENT-{unique_suffix}",
                name="Inventory management product",
            )
            provider = InventoryProviderModel(
                id=provider_id,
                name=f"inventory-management-{unique_suffix}",
                kind=ProviderKind.INTERNAL,
                driver="internal",
            )
            session.add_all((product, provider))
            await session.flush()

        repository = SqlAlchemyInventoryLevelRepository(database)

        first_result = await repository.set_level(
            product_id=product_id,
            provider_id=provider_id,
            on_hand=12,
            allocation_priority=10,
        )
        repeated_result = await repository.set_level(
            product_id=product_id,
            provider_id=provider_id,
            on_hand=12,
            allocation_priority=10,
        )
        updated_result = await repository.set_level(
            product_id=product_id,
            provider_id=provider_id,
            on_hand=20,
            allocation_priority=5,
        )
        retrieved_result = await repository.get_level(
            product_id=product_id,
            provider_id=provider_id,
        )

        expected = InventoryLevel(
            product_id=product_id,
            provider_id=provider_id,
            on_hand=12,
            reserved=0,
            allocation_priority=10,
            version=1,
        )
        assert (
            first_result,
            repeated_result,
            updated_result,
            retrieved_result,
        ) == (
            expected,
            expected,
            InventoryLevel(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=20,
                reserved=0,
                allocation_priority=5,
                version=2,
            ),
            InventoryLevel(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=20,
                reserved=0,
                allocation_priority=5,
                version=2,
            ),
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryLevelModel).where(
                    InventoryLevelModel.product_id == product_id,
                    InventoryLevelModel.provider_id == provider_id,
                )
            )
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
            await session.execute(
                delete(ProductModel).where(ProductModel.id == product_id)
            )
        await database.close()


@pytest.mark.integration
async def test_product_inventory_is_listed_in_allocation_order() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    unique_suffix = uuid7().hex
    product_id = uuid7()
    first_provider_id = uuid7()
    second_provider_id = uuid7()

    try:
        async with database.session() as session, session.begin():
            session.add_all(
                (
                    ProductModel(
                        id=product_id,
                        sku=f"INVENTORY-LIST-{unique_suffix}",
                        name="Inventory list product",
                    ),
                    InventoryProviderModel(
                        id=first_provider_id,
                        name=f"inventory-list-first-{unique_suffix}",
                        kind=ProviderKind.INTERNAL,
                        driver="internal",
                    ),
                    InventoryProviderModel(
                        id=second_provider_id,
                        name=f"inventory-list-second-{unique_suffix}",
                        kind=ProviderKind.INTERNAL,
                        driver="internal",
                    ),
                )
            )
            await session.flush()
            session.add_all(
                (
                    InventoryLevelModel(
                        product_id=product_id,
                        provider_id=first_provider_id,
                        on_hand=12,
                        reserved=2,
                        allocation_priority=20,
                        version=3,
                    ),
                    InventoryLevelModel(
                        product_id=product_id,
                        provider_id=second_provider_id,
                        on_hand=8,
                        reserved=1,
                        allocation_priority=10,
                        version=2,
                    ),
                )
            )

        levels = await SqlAlchemyInventoryLevelRepository(
            database
        ).list_by_product(product_id)

        assert levels == (
            InventoryLevel(
                product_id=product_id,
                provider_id=second_provider_id,
                on_hand=8,
                reserved=1,
                allocation_priority=10,
                version=2,
            ),
            InventoryLevel(
                product_id=product_id,
                provider_id=first_provider_id,
                on_hand=12,
                reserved=2,
                allocation_priority=20,
                version=3,
            ),
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryLevelModel).where(
                    InventoryLevelModel.product_id == product_id
                )
            )
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id.in_(
                        (first_provider_id, second_provider_id)
                    )
                )
            )
            await session.execute(
                delete(ProductModel).where(ProductModel.id == product_id)
            )
        await database.close()


@pytest.mark.integration
async def test_inventory_cannot_be_persisted_below_reserved_quantity() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    unique_suffix = uuid7().hex
    product_id = uuid7()
    provider_id = uuid7()

    try:
        async with database.session() as session, session.begin():
            session.add_all(
                (
                    ProductModel(
                        id=product_id,
                        sku=f"INVENTORY-RESERVED-{unique_suffix}",
                        name="Reserved inventory product",
                    ),
                    InventoryProviderModel(
                        id=provider_id,
                        name=f"inventory-reserved-{unique_suffix}",
                        kind=ProviderKind.INTERNAL,
                        driver="internal",
                    ),
                )
            )
            await session.flush()
            session.add(
                InventoryLevelModel(
                    product_id=product_id,
                    provider_id=provider_id,
                    on_hand=12,
                    reserved=5,
                    allocation_priority=10,
                    version=2,
                )
            )

        repository = SqlAlchemyInventoryLevelRepository(database)

        with pytest.raises(InventoryBelowReservedError) as captured:
            await repository.set_level(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=4,
                allocation_priority=10,
            )

        current = await repository.set_level(
            product_id=product_id,
            provider_id=provider_id,
            on_hand=12,
            allocation_priority=10,
        )

        assert (
            captured.value.product_id,
            captured.value.provider_id,
            captured.value.reserved,
            current,
        ) == (
            product_id,
            provider_id,
            5,
            InventoryLevel(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=12,
                reserved=5,
                allocation_priority=10,
                version=2,
            ),
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryLevelModel).where(
                    InventoryLevelModel.product_id == product_id,
                    InventoryLevelModel.provider_id == provider_id,
                )
            )
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
            await session.execute(
                delete(ProductModel).where(ProductModel.id == product_id)
            )
        await database.close()
