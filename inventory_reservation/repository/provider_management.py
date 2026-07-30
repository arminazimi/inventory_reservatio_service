from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryProviderModel,
    ProviderCredentialModel,
)
from inventory_reservation.repository.models import (
    ProviderAuthType as ProviderAuthTypeModel,
)
from inventory_reservation.repository.models import (
    ProviderKind as ProviderKindModel,
)
from inventory_reservation.service.provider_management import (
    ProviderAuthType,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderCredentialConfiguration,
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

    async def update(self, provider: ProviderConfiguration) -> bool:
        statement = (
            update(InventoryProviderModel)
            .where(InventoryProviderModel.id == provider.id)
            .values(
                name=provider.name,
                base_url=provider.base_url,
                request_timeout_ms=provider.request_timeout_ms,
                supports_availability=provider.capabilities.availability,
                supports_hold=provider.capabilities.hold,
                supports_confirm=provider.capabilities.confirm,
                supports_release=provider.capabilities.release,
            )
            .returning(InventoryProviderModel.id)
        )
        try:
            async with self._database.session() as session, session.begin():
                updated_provider_id = (
                    await session.scalars(statement)
                ).one_or_none()
        except IntegrityError as error:
            if _violates_constraint(
                error,
                PROVIDER_NAME_CONSTRAINT,
            ):
                raise ProviderNameConflictError(provider.name) from error
            raise

        return updated_provider_id is not None

    async def set_enabled(
        self,
        provider_id: UUID,
        *,
        is_enabled: bool,
    ) -> ProviderConfiguration | None:
        statement = (
            update(InventoryProviderModel)
            .where(InventoryProviderModel.id == provider_id)
            .values(is_enabled=is_enabled)
            .returning(InventoryProviderModel)
        )
        async with self._database.session() as session, session.begin():
            provider = (await session.scalars(statement)).one_or_none()

        if provider is None:
            return None
        return self._to_domain(provider)

    async def upsert_credentials(
        self,
        credentials: ProviderCredentialConfiguration,
    ) -> ProviderCredentialConfiguration:
        insert_statement = insert(ProviderCredentialModel).values(
            provider_id=credentials.provider_id,
            auth_type=ProviderAuthTypeModel(credentials.auth_type.value),
            secret_ref=credentials.secret_ref,
            public_config=credentials.public_config,
        )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[ProviderCredentialModel.provider_id],
            set_={
                "auth_type": insert_statement.excluded.auth_type,
                "secret_ref": insert_statement.excluded.secret_ref,
                "public_config": insert_statement.excluded.public_config,
                "updated_at": func.now(),
            },
        ).returning(ProviderCredentialModel)
        async with self._database.session() as session, session.begin():
            credential_model = (await session.scalars(statement)).one()

        return self._credentials_to_domain(credential_model)

    async def get_credentials(
        self,
        provider_id: UUID,
    ) -> ProviderCredentialConfiguration | None:
        statement = select(ProviderCredentialModel).where(
            ProviderCredentialModel.provider_id == provider_id
        )
        async with self._database.session() as session:
            credential_model = (
                await session.scalars(statement)
            ).one_or_none()

        if credential_model is None:
            return None
        return self._credentials_to_domain(credential_model)

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

    @staticmethod
    def _credentials_to_domain(
        credentials: ProviderCredentialModel,
    ) -> ProviderCredentialConfiguration:
        return ProviderCredentialConfiguration(
            provider_id=credentials.provider_id,
            auth_type=ProviderAuthType(credentials.auth_type.value),
            secret_ref=credentials.secret_ref,
            public_config=credentials.public_config,
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
