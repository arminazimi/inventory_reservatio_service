from uuid import UUID

from sqlalchemy import select

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import InventoryProviderModel
from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
)


class SqlAlchemyProviderRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list(self) -> tuple[ProviderConfiguration, ...]:
        statement = select(InventoryProviderModel).order_by(
            InventoryProviderModel.name,
            InventoryProviderModel.id,
        )
        async with self._database.session() as session:
            providers = (await session.scalars(statement)).all()

        return tuple(self._to_domain(provider) for provider in providers)

    async def get(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration | None:
        statement = select(InventoryProviderModel).where(
            InventoryProviderModel.id == provider_id
        )
        async with self._database.session() as session:
            provider = (await session.scalars(statement)).one_or_none()

        if provider is None:
            return None
        return self._to_domain(provider)

    @staticmethod
    def _to_domain(provider: InventoryProviderModel) -> ProviderConfiguration:
        return ProviderConfiguration(
            id=provider.id,
            name=provider.name,
            kind=ProviderKind(provider.kind.value),
            driver=provider.driver,
            base_url=provider.base_url,
            request_timeout_ms=provider.request_timeout_ms,
            capabilities=ProviderCapabilities(
                availability=provider.supports_availability,
                hold=provider.supports_hold,
                confirm=provider.supports_confirm,
                release=provider.supports_release,
            ),
            is_enabled=provider.is_enabled,
        )
