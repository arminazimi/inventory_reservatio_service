import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "alembic_version",
    "inventory_allocations",
    "inventory_levels",
    "inventory_providers",
    "order_items",
    "orders",
    "products",
    "provider_credentials",
    "provider_operations",
    "reservation_items",
    "reservations",
}


@pytest.mark.integration
async def test_latest_migration_creates_the_complete_schema() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
    )
    alembic_config = Config(PROJECT_ROOT / "alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)

    await asyncio.to_thread(command.upgrade, alembic_config, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
    finally:
        await engine.dispose()

    assert table_names == EXPECTED_TABLES
