import os
from uuid import uuid7

import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryProviderModel,
)
from inventory_reservation.repository.models import (
    ProviderKind as ProviderKindModel,
)
from inventory_reservation.repository.provider_management import (
    SqlAlchemyProviderRepository,
)
from inventory_reservation.service.provider_management import (
    ProviderCapabilities,
    ProviderConfiguration,
    ProviderKind,
)


@pytest.mark.integration
async def test_repository_lists_provider_configurations_in_name_order() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    suffix = uuid7().hex
    alpha_id = uuid7()
    zulu_id = uuid7()

    try:
        async with database.session() as session, session.begin():
            session.add_all(
                [
                    InventoryProviderModel(
                        id=zulu_id,
                        name=f"zulu-{suffix}",
                        kind=ProviderKindModel.INTERNAL,
                        driver="internal",
                        request_timeout_ms=2_000,
                        supports_availability=True,
                        supports_hold=True,
                        supports_confirm=True,
                        supports_release=True,
                        is_enabled=False,
                    ),
                    InventoryProviderModel(
                        id=alpha_id,
                        name=f"alpha-{suffix}",
                        kind=ProviderKindModel.EXTERNAL,
                        driver="http",
                        base_url="https://inventory.provider.test",
                        request_timeout_ms=1_500,
                        supports_availability=True,
                        supports_hold=True,
                        supports_confirm=False,
                        supports_release=True,
                        is_enabled=True,
                    ),
                ]
            )

        providers = await SqlAlchemyProviderRepository(database).list()
        created_providers = tuple(
            provider
            for provider in providers
            if provider.id in {alpha_id, zulu_id}
        )

        assert created_providers == (
            ProviderConfiguration(
                id=alpha_id,
                name=f"alpha-{suffix}",
                kind=ProviderKind.EXTERNAL,
                driver="http",
                base_url="https://inventory.provider.test",
                request_timeout_ms=1_500,
                capabilities=ProviderCapabilities(
                    availability=True,
                    hold=True,
                    confirm=False,
                    release=True,
                ),
                is_enabled=True,
            ),
            ProviderConfiguration(
                id=zulu_id,
                name=f"zulu-{suffix}",
                kind=ProviderKind.INTERNAL,
                driver="internal",
                base_url=None,
                request_timeout_ms=2_000,
                capabilities=ProviderCapabilities(
                    availability=True,
                    hold=True,
                    confirm=True,
                    release=True,
                ),
                is_enabled=False,
            ),
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id.in_((alpha_id, zulu_id))
                )
            )
        await database.close()
