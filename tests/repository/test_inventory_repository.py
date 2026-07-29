import asyncio
import os
from uuid import uuid7

import pytest
from sqlalchemy import delete

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


@pytest.mark.integration
async def test_try_hold_keeps_inventory_unchanged_when_stock_is_insufficient() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        async with database.session() as session:
            try:
                product = ProductModel(
                    sku="INSUFFICIENT-HOLD-TEST",
                    name="Insufficient hold test product",
                )
                provider = InventoryProviderModel(
                    name="internal-insufficient-hold-test",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                    supports_confirm=True,
                    supports_release=True,
                )
                session.add_all([product, provider])
                await session.flush()

                session.add(
                    InventoryLevelModel(
                        product_id=product.id,
                        provider_id=provider.id,
                        on_hand=5,
                        reserved=1,
                    )
                )
                await session.flush()

                repository = InventoryRepository(session)
                rejected_hold = await repository.try_hold(
                    product_id=product.id,
                    provider_id=provider.id,
                    quantity=5,
                )
                current_inventory = await repository.get_snapshot(
                    product_id=product.id,
                    provider_id=provider.id,
                )

                assert (rejected_hold, current_inventory) == (
                    None,
                    InventorySnapshot(
                        product_id=product.id,
                        provider_id=provider.id,
                        on_hand=5,
                        reserved=1,
                        version=1,
                    ),
                )
            finally:
                await session.rollback()
    finally:
        await database.close()


@pytest.mark.integration
async def test_concurrent_holds_do_not_oversell_inventory() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    unique_suffix = uuid7().hex

    try:
        async with database.session() as session, session.begin():
            product = ProductModel(
                sku=f"CONCURRENT-HOLD-{unique_suffix}",
                name="Concurrent hold test product",
            )
            provider = InventoryProviderModel(
                name=f"internal-concurrent-hold-{unique_suffix}",
                kind=ProviderKind.INTERNAL,
                driver="internal",
                supports_hold=True,
                supports_confirm=True,
                supports_release=True,
            )
            session.add_all([product, provider])
            await session.flush()

            session.add(
                InventoryLevelModel(
                    product_id=product.id,
                    provider_id=provider.id,
                    on_hand=5,
                    reserved=0,
                )
            )

        product_id = product.id
        provider_id = provider.id

        try:
            start_barrier = asyncio.Barrier(3)

            async def attempt_hold() -> InventorySnapshot | None:
                async with database.session() as session:
                    await start_barrier.wait()
                    async with session.begin():
                        return await InventoryRepository(session).try_hold(
                            product_id=product_id,
                            provider_id=provider_id,
                            quantity=4,
                        )

            attempts = [
                asyncio.create_task(attempt_hold()),
                asyncio.create_task(attempt_hold()),
            ]
            await start_barrier.wait()

            async with asyncio.timeout(10):
                results = await asyncio.gather(*attempts)

            async with database.session() as session:
                current_inventory = await InventoryRepository(session).get_snapshot(
                    product_id=product_id,
                    provider_id=provider_id,
                )

            assert (
                sum(result is not None for result in results),
                current_inventory,
            ) == (
                1,
                InventorySnapshot(
                    product_id=product_id,
                    provider_id=provider_id,
                    on_hand=5,
                    reserved=4,
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
    finally:
        await database.close()
