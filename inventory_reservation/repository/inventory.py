from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProviderCredentialModel,
    ProviderKind,
)
from inventory_reservation.repository.provider import ProviderRegistry
from inventory_reservation.service.provider import (
    HoldCommand,
    HoldProvider,
    ProviderHold,
    ProviderHoldAttempt,
    ProviderRouter,
)
from inventory_reservation.service.provider_management import (
    ProviderAuthType,
    ProviderCredentialConfiguration,
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
    def __init__(
        self,
        session: AsyncSession,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        self._session = session
        self._provider_registry = provider_registry

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

    async def confirm_hold(
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
                InventoryLevelModel.on_hand >= quantity,
                InventoryLevelModel.reserved >= quantity,
            )
            .values(
                on_hand=InventoryLevelModel.on_hand - quantity,
                reserved=InventoryLevelModel.reserved - quantity,
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

    async def release_hold(
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
                InventoryLevelModel.reserved >= quantity,
            )
            .values(
                reserved=InventoryLevelModel.reserved - quantity,
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

    async def try_hold_available(
        self,
        *,
        product_id: UUID,
        quantity: int,
        idempotency_key: str,
    ) -> ProviderHold | None:
        candidates_statement = (
            select(
                InventoryProviderModel,
                ProviderCredentialModel,
            )
            .select_from(InventoryLevelModel)
            .join(
                InventoryProviderModel,
                InventoryProviderModel.id == InventoryLevelModel.provider_id,
            )
            .outerjoin(
                ProviderCredentialModel,
                ProviderCredentialModel.provider_id
                == InventoryProviderModel.id,
            )
            .where(
                InventoryLevelModel.product_id == product_id,
                InventoryProviderModel.is_enabled.is_(True),
                InventoryProviderModel.supports_hold.is_(True),
            )
            .order_by(
                InventoryLevelModel.allocation_priority,
                InventoryLevelModel.provider_id,
            )
        )
        candidates = (
            await self._session.execute(candidates_statement)
        ).all()

        router = ProviderRouter(
            tuple(
                provider
                for candidate, credentials in candidates
                if (
                    provider := self._hold_provider(
                        candidate,
                        credentials,
                    )
                )
                is not None
            )
        )
        return await router.hold(
            HoldCommand(
                product_id=product_id,
                quantity=quantity,
                idempotency_key=idempotency_key,
            )
        )

    def _hold_provider(
        self,
        candidate: InventoryProviderModel,
        credentials: ProviderCredentialModel | None,
    ) -> HoldProvider | None:
        if candidate.kind is ProviderKind.INTERNAL:
            return _InternalInventoryProvider(
                provider_id=candidate.id,
                inventory_repository=self,
            )
        if (
            self._provider_registry is not None
            and candidate.base_url is not None
            and candidate.driver == "http"
        ):
            return self._provider_registry.get_external(
                provider_id=candidate.id,
                base_url=candidate.base_url,
                timeout=candidate.request_timeout_ms / 1000,
                credentials=(
                    ProviderCredentialConfiguration(
                        provider_id=credentials.provider_id,
                        auth_type=ProviderAuthType(
                            credentials.auth_type.value
                        ),
                        secret_ref=credentials.secret_ref,
                        public_config=credentials.public_config,
                    )
                    if credentials is not None
                    else None
                ),
            )
        return None


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
