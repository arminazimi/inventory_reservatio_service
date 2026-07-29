import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from inventory_reservation.repository.models import ProductModel


@pytest.mark.integration
async def test_new_records_receive_time_ordered_uuid7_ids() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
    )
    engine = create_async_engine(database_url)

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                product = ProductModel(sku="UUID7-TEST", name="UUIDv7 test product")
                session.add(product)
                await session.flush()

                assert product.id.version == 7
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
