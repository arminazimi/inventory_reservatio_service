from typing import cast

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class Database:
    """Own the SQLAlchemy engine lifecycle and expose database readiness."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_pre_ping=True,
        )

    async def is_ready(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(text("SELECT 1"))
                return cast(int, result.scalar_one()) == 1
        except SQLAlchemyError:
            return False

    async def close(self) -> None:
        await self._engine.dispose()
