from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryProviderModel,
)
from inventory_reservation.repository.models import (
    ProviderKind as ProviderKindModel,
)
from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
    ProviderNameConflictError,
)

PROVIDER_NAME_CONSTRAINT = "uq_inventory_providers_name"


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

    async def add(self, provider: ProviderConfiguration) -> None:
        try:
            async with self._database.session() as session, session.begin():
                session.add(self._to_model(provider))
        except IntegrityError as error:
            if _violates_constraint(
                error,
                PROVIDER_NAME_CONSTRAINT,
            ):
                raise ProviderNameConflictError(provider.name) from error
            raise

    @staticmethod
    def _to_model(provider: ProviderConfiguration) -> InventoryProviderModel:
        return InventoryProviderModel(
            id=provider.id,
            name=provider.name,
            kind=ProviderKindModel(provider.kind.value),
            driver=provider.driver,
            base_url=provider.base_url,
            request_timeout_ms=provider.request_timeout_ms,
            supports_availability=provider.capabilities.availability,
            supports_hold=provider.capabilities.hold,
            supports_confirm=provider.capabilities.confirm,
            supports_release=provider.capabilities.release,
            is_enabled=provider.is_enabled,
        )

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


def _violates_constraint(
    error: IntegrityError,
    constraint_name: str,
) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if getattr(cause, "constraint_name", None) == constraint_name:
            return True
        cause = cause.__cause__
    return False
