from uuid import UUID, uuid7

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryProviderModel,
    ProductModel,
    ProductOfferModel,
)
from inventory_reservation.service.inventory_management import (
    InventoryBelowReservedError,
    InventoryHasActiveReservationsError,
    InventoryLevel,
)


class SqlAlchemyInventoryLevelRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def product_exists(self, product_id: UUID) -> bool:
        statement = select(ProductModel.id).where(ProductModel.id == product_id).limit(1)
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

    async def list_by_product(
        self,
        product_id: UUID,
    ) -> tuple[InventoryLevel, ...]:
        statement = (
            select(ProductOfferModel)
            .where(ProductOfferModel.product_id == product_id)
            .order_by(
                ProductOfferModel.allocation_priority,
                ProductOfferModel.provider_id,
            )
        )
        async with self._database.session() as session:
            levels = (await session.scalars(statement)).all()
        return tuple(self._to_domain(level) for level in levels)

    async def get_level(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
    ) -> InventoryLevel | None:
        statement = select(ProductOfferModel).where(
            ProductOfferModel.product_id == product_id,
            ProductOfferModel.provider_id == provider_id,
        )
        async with self._database.session() as session:
            level = (await session.scalars(statement)).one_or_none()
        if level is None:
            return None
        return self._to_domain(level)

    async def remove_level(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
    ) -> bool:
        delete_statement = (
            delete(ProductOfferModel)
            .where(
                ProductOfferModel.product_id == product_id,
                ProductOfferModel.provider_id == provider_id,
                ProductOfferModel.reserved == 0,
            )
            .returning(ProductOfferModel.id)
        )
        async with self._database.session() as session, session.begin():
            removed_id = await session.scalar(delete_statement)
            if removed_id is not None:
                return True

            current = (
                await session.scalars(
                    select(ProductOfferModel).where(
                        ProductOfferModel.product_id == product_id,
                        ProductOfferModel.provider_id == provider_id,
                    )
                )
            ).one_or_none()
            if current is None:
                return False
            raise InventoryHasActiveReservationsError(
                product_id=product_id,
                provider_id=provider_id,
                reserved=current.reserved,
            )

    async def set_level(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
        on_hand: int,
        allocation_priority: int,
        routing_group: UUID | None = None,
    ) -> InventoryLevel:
        insert_statement = insert(ProductOfferModel).values(
            id=uuid7(),
            product_id=product_id,
            provider_id=provider_id,
            on_hand=on_hand,
            reserved=0,
            allocation_priority=allocation_priority,
            routing_group=routing_group,
            version=1,
        )
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=[
                ProductOfferModel.product_id,
                ProductOfferModel.provider_id,
            ],
            set_={
                "on_hand": insert_statement.excluded.on_hand,
                "allocation_priority": (insert_statement.excluded.allocation_priority),
                    "version": ProductOfferModel.version + 1,
                    "routing_group": insert_statement.excluded.routing_group,
            },
            where=and_(
                ProductOfferModel.reserved <= insert_statement.excluded.on_hand,
                or_(
                    ProductOfferModel.on_hand != insert_statement.excluded.on_hand,
                        ProductOfferModel.allocation_priority
                        != insert_statement.excluded.allocation_priority,
                        ProductOfferModel.routing_group.is_distinct_from(
                            insert_statement.excluded.routing_group
                        ),
                ),
            ),
        ).returning(ProductOfferModel)

        async with self._database.session() as session, session.begin():
            level = (await session.scalars(upsert_statement)).one_or_none()
            if level is None:
                level = (
                    await session.scalars(
                        select(ProductOfferModel).where(
                            ProductOfferModel.product_id == product_id,
                            ProductOfferModel.provider_id == provider_id,
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
    def _to_domain(level: ProductOfferModel) -> InventoryLevel:
        return InventoryLevel(
            product_id=level.product_id,
            provider_id=level.provider_id,
            on_hand=level.on_hand,
            reserved=level.reserved,
            allocation_priority=level.allocation_priority,
            version=level.version,
            routing_group=level.routing_group,
        )
