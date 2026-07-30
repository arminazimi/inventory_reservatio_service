import os

import pytest

from inventory_reservation.repository.database import Database


@pytest.mark.integration
async def test_database_is_ready_when_postgres_accepts_connections() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )

    try:
        assert await database.is_ready() is True
    finally:
        await database.close()


async def test_database_is_not_ready_when_connection_cannot_be_opened() -> None:
    database = Database(
        "postgresql+asyncpg://inventory:inventory@/inventory"
        "?host=/tmp/inventory-reservation-missing-postgres"
    )

    try:
        assert await database.is_ready() is False
    finally:
        await database.close()
