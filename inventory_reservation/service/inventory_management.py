from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from inventory_reservation.service.product import ProductNotFoundError
from inventory_reservation.service.provider_management import (
    ProviderNotFoundError,
)


@dataclass(frozen=True, slots=True)
class InventoryLevel:
    product_id: UUID
    provider_id: UUID
    on_hand: int
    reserved: int
    allocation_priority: int
    version: int

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass(frozen=True, slots=True)
class SetInventoryLevelCommand:
    product_id: UUID
    provider_id: UUID
    on_hand: int
    allocation_priority: int


class InventoryBelowReservedError(ValueError):
    def __init__(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
        reserved: int,
    ) -> None:
        self.product_id = product_id
        self.provider_id = provider_id
        self.reserved = reserved
        super().__init__(
            "On-hand inventory cannot be lower than reserved inventory."
        )


class InventoryLevelRepositoryPort(Protocol):
    async def product_exists(self, product_id: UUID) -> bool: ...

    async def provider_exists(self, provider_id: UUID) -> bool: ...

    async def set_level(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
        on_hand: int,
        allocation_priority: int,
    ) -> InventoryLevel: ...


class InventoryManagementService:
    def __init__(
        self,
        *,
        repository: InventoryLevelRepositoryPort,
    ) -> None:
        self._repository = repository

    async def set_inventory_level(
        self,
        command: SetInventoryLevelCommand,
    ) -> InventoryLevel:
        if not await self._repository.product_exists(command.product_id):
            raise ProductNotFoundError(command.product_id)
        if not await self._repository.provider_exists(command.provider_id):
            raise ProviderNotFoundError(command.provider_id)
        return await self._repository.set_level(
            product_id=command.product_id,
            provider_id=command.provider_id,
            on_hand=command.on_hand,
            allocation_priority=command.allocation_priority,
        )
