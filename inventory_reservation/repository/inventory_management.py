from uuid import UUID, uuid7

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProductModel,
)
from inventory_reservation.service.inventory_management import (
    InventoryBelowReservedError,
    InventoryLevel,
)


class SqlAlchemyInventoryLevelRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def product_exists(self, product_id: UUID) -> bool:
        statement = (
            select(ProductModel.id)
            .where(ProductModel.id == product_id)
            .limit(1)
        )
        async with self._database.session() as session:
            return (await session.scalar(statement)) is not None

    async def provider_exists(self, provider_id: UUID) -> bool:
        statement = (
            select(InventoryProviderModel.id)
            .where(InventoryProviderModel.id == provider_id)
            .limit(1)
        )
        async with self._database.session() as session:
            return (await session.scalar(statement)) is not None

    async def set_level(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
        on_hand: int,
        allocation_priority: int,
    ) -> InventoryLevel:
        insert_statement = insert(InventoryLevelModel).values(
            id=uuid7(),
            product_id=product_id,
            provider_id=provider_id,
            on_hand=on_hand,
            reserved=0,
            allocation_priority=allocation_priority,
            version=1,
        )
        upsert_statement = (
            insert_statement.on_conflict_do_update(
                index_elements=[
                    InventoryLevelModel.product_id,
                    InventoryLevelModel.provider_id,
                ],
                set_={
                    "on_hand": insert_statement.excluded.on_hand,
                    "allocation_priority": (
                        insert_statement.excluded.allocation_priority
                    ),
                    "version": InventoryLevelModel.version + 1,
                },
                where=and_(
                    InventoryLevelModel.reserved
                    <= insert_statement.excluded.on_hand,
                    or_(
                        InventoryLevelModel.on_hand
                        != insert_statement.excluded.on_hand,
                        InventoryLevelModel.allocation_priority
                        != insert_statement.excluded.allocation_priority,
                    ),
                ),
            )
            .returning(InventoryLevelModel)
        )

        async with self._database.session() as session, session.begin():
            level = (
                await session.scalars(upsert_statement)
            ).one_or_none()
            if level is None:
                level = (
                    await session.scalars(
                        select(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id,
                            InventoryLevelModel.provider_id == provider_id,
                        )
                    )
                ).one()
                if level.reserved > on_hand:
                    raise InventoryBelowReservedError(
                        product_id=product_id,
                        provider_id=provider_id,
                        reserved=level.reserved,
                    )

        return self._to_domain(level)

    @staticmethod
    def _to_domain(level: InventoryLevelModel) -> InventoryLevel:
        return InventoryLevel(
            product_id=level.product_id,
            provider_id=level.provider_id,
            on_hand=level.on_hand,
            reserved=level.reserved,
            allocation_priority=level.allocation_priority,
            version=level.version,
        )
