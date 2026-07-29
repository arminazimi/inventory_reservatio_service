from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_reservation.repository.models import InventoryLevelModel


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    product_id: UUID
    provider_id: UUID
    on_hand: int
    reserved: int
    version: int

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_hold(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
        quantity: int,
    ) -> InventorySnapshot | None:
        statement = (
            update(InventoryLevelModel)
            .where(
                InventoryLevelModel.product_id == product_id,
                InventoryLevelModel.provider_id == provider_id,
                InventoryLevelModel.on_hand - InventoryLevelModel.reserved >= quantity,
            )
            .values(
                reserved=InventoryLevelModel.reserved + quantity,
                version=InventoryLevelModel.version + 1,
            )
            .returning(InventoryLevelModel)
        )
        level = (await self._session.scalars(statement)).one_or_none()

        if level is None:
            return None

        return InventorySnapshot(
            product_id=level.product_id,
            provider_id=level.provider_id,
            on_hand=level.on_hand,
            reserved=level.reserved,
            version=level.version,
        )
