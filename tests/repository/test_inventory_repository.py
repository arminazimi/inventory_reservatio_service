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
    InventoryProviderModel,
    ProductModel,
    ProductOfferModel,
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

                level = ProductOfferModel(
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
                    ProductOfferModel(
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
async def test_try_hold_selected_reserves_only_the_user_selected_provider() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        async with database.session() as session:
            try:
                product = ProductModel(sku="SELECTED-OFFER", name="Selected offer")
                first_provider = InventoryProviderModel(
                    name="selected-offer-first",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                selected_provider = InventoryProviderModel(
                    name="selected-offer-second",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                session.add_all([product, first_provider, selected_provider])
                await session.flush()
                session.add_all(
                    [
                        ProductOfferModel(
                            product_id=product.id,
                            provider_id=first_provider.id,
                            on_hand=10,
                            allocation_priority=1,
                        ),
                        ProductOfferModel(
                            product_id=product.id,
                            provider_id=selected_provider.id,
                            on_hand=10,
                            allocation_priority=100,
                        ),
                    ]
                )
                await session.flush()

                repository = InventoryRepository(session)
                hold = await repository.try_hold_selected(
                    product_id=product.id,
                    provider_id=selected_provider.id,
                    quantity=2,
                    idempotency_key="selected-offer-hold",
                )

                assert hold is not None
                assert hold.provider_id == selected_provider.id
                assert await repository.get_snapshot(
                    product_id=product.id,
                    provider_id=first_provider.id,
                ) == InventorySnapshot(
                    product_id=product.id,
                    provider_id=first_provider.id,
                    on_hand=10,
                    reserved=0,
                    version=1,
                )
                assert await repository.get_snapshot(
                    product_id=product.id,
                    provider_id=selected_provider.id,
                ) == InventorySnapshot(
                    product_id=product.id,
                    provider_id=selected_provider.id,
                    on_hand=10,
                    reserved=2,
                    version=2,
                )
            finally:
                await session.rollback()
    finally:
        await database.close()


@pytest.mark.integration
async def test_selected_offer_falls_back_only_inside_its_routing_group() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    routing_group = uuid7()

    try:
        async with database.session() as session:
            try:
                product = ProductModel(sku=f"ROUTING-{uuid7().hex}", name="Routed offer")
                primary = InventoryProviderModel(
                    name=f"routing-primary-{uuid7().hex}",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                fallback = InventoryProviderModel(
                    name=f"routing-fallback-{uuid7().hex}",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                unrelated = InventoryProviderModel(
                    name=f"routing-unrelated-{uuid7().hex}",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                session.add_all([product, primary, fallback, unrelated])
                await session.flush()
                session.add_all(
                    [
                        ProductOfferModel(
                            product_id=product.id,
                            provider_id=primary.id,
                            on_hand=0,
                            allocation_priority=10,
                            routing_group=routing_group,
                        ),
                        ProductOfferModel(
                            product_id=product.id,
                            provider_id=fallback.id,
                            on_hand=5,
                            allocation_priority=20,
                            routing_group=routing_group,
                        ),
                        ProductOfferModel(
                            product_id=product.id,
                            provider_id=unrelated.id,
                            on_hand=5,
                            allocation_priority=1,
                        ),
                    ]
                )
                await session.flush()

                hold = await InventoryRepository(session).try_hold_selected(
                    product_id=product.id,
                    provider_id=primary.id,
                    quantity=2,
                    idempotency_key="routing-group-hold",
                )

                assert hold is not None
                assert hold.provider_id == fallback.id
                assert (
                    await InventoryRepository(session).get_snapshot(
                        product_id=product.id,
                        provider_id=unrelated.id,
                    )
                ).reserved == 0
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
                ProductOfferModel(
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
                    delete(ProductOfferModel).where(
                        ProductOfferModel.product_id == product_id,
                        ProductOfferModel.provider_id == provider_id,
                    )
                )
                await session.execute(
                    delete(InventoryProviderModel).where(InventoryProviderModel.id == provider_id)
                )
                await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()
