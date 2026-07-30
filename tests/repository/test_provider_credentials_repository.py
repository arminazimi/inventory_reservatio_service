import os
from uuid import uuid7

import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import InventoryProviderModel
from inventory_reservation.repository.provider_management import (
    SqlAlchemyProviderRepository,
)
from inventory_reservation.service.provider_management import (
    ProviderAuthType,
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderCredentialConfiguration,
    ProviderKind,
)


@pytest.mark.integration
async def test_repository_upserts_provider_credentials() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name=f"provider-credentials-{provider_id.hex}",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://provider-credentials.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    original_credentials = ProviderCredentialConfiguration(
        provider_id=provider_id,
        auth_type=ProviderAuthType.BEARER,
        secret_ref="vault://inventory/original",
        public_config={"scheme": "Bearer"},
    )
    updated_credentials = ProviderCredentialConfiguration(
        provider_id=provider_id,
        auth_type=ProviderAuthType.API_KEY,
        secret_ref="vault://inventory/rotated",
        public_config={"header_name": "X-API-Key"},
    )

    try:
        repository = SqlAlchemyProviderRepository(database)
        await repository.add(provider)
        await repository.upsert_credentials(original_credentials)

        await repository.upsert_credentials(updated_credentials)

        assert (
            await repository.get_credentials(provider_id)
            == updated_credentials
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
        await database.close()
