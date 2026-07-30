from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    """Own the SQLAlchemy engine lifecycle and expose database readiness."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    async def is_ready(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(text("SELECT 1"))
                return cast(int, result.scalar_one()) == 1
        except (OSError, SQLAlchemyError):
            return False

    async def close(self) -> None:
        await self._engine.dispose()
