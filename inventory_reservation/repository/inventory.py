from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProviderKind,
)
from inventory_reservation.service.provider import (
    HoldCommand,
    ProviderHold,
    ProviderHoldAttempt,
    ProviderRouter,
)


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

    async def get_snapshot(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
    ) -> InventorySnapshot | None:
        statement = select(InventoryLevelModel).where(
            InventoryLevelModel.product_id == product_id,
            InventoryLevelModel.provider_id == provider_id,
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

    async def try_hold_internal(
        self,
        *,
        product_id: UUID,
        quantity: int,
        idempotency_key: str,
    ) -> ProviderHold | None:
        candidates_statement = (
            select(InventoryLevelModel.provider_id)
            .join(
                InventoryProviderModel,
                InventoryProviderModel.id == InventoryLevelModel.provider_id,
            )
            .where(
                InventoryLevelModel.product_id == product_id,
                InventoryProviderModel.kind == ProviderKind.INTERNAL,
                InventoryProviderModel.is_enabled.is_(True),
                InventoryProviderModel.supports_hold.is_(True),
            )
            .order_by(
                InventoryLevelModel.allocation_priority,
                InventoryLevelModel.provider_id,
            )
        )
        provider_ids = (await self._session.scalars(candidates_statement)).all()

        router = ProviderRouter(
            tuple(
                _InternalInventoryProvider(
                    provider_id=provider_id,
                    inventory_repository=self,
                )
                for provider_id in provider_ids
            )
        )
        return await router.hold(
            HoldCommand(
                product_id=product_id,
                quantity=quantity,
                idempotency_key=idempotency_key,
            )
        )


@dataclass(frozen=True, slots=True)
class _InternalInventoryProvider:
    provider_id: UUID
    inventory_repository: InventoryRepository

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt:
        snapshot = await self.inventory_repository.try_hold(
            product_id=command.product_id,
            provider_id=self.provider_id,
            quantity=command.quantity,
        )
        if snapshot is None:
            return ProviderHoldAttempt.out_of_stock()
        return ProviderHoldAttempt.held()
