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
    ProviderNameConflictError,
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


@pytest.mark.integration
async def test_repository_gets_provider_configuration_by_id() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    provider_id = uuid7()
    suffix = provider_id.hex

    try:
        async with database.session() as session, session.begin():
            session.add(
                InventoryProviderModel(
                    id=provider_id,
                    name=f"provider-get-{suffix}",
                    kind=ProviderKindModel.EXTERNAL,
                    driver="http",
                    base_url="https://provider-get.test",
                    request_timeout_ms=750,
                    supports_availability=True,
                    supports_hold=True,
                    supports_confirm=True,
                    supports_release=False,
                    is_enabled=True,
                )
            )

        provider = await SqlAlchemyProviderRepository(database).get(provider_id)

        assert provider == ProviderConfiguration(
            id=provider_id,
            name=f"provider-get-{suffix}",
            kind=ProviderKind.EXTERNAL,
            driver="http",
            base_url="https://provider-get.test",
            request_timeout_ms=750,
            capabilities=ProviderCapabilities(
                availability=True,
                hold=True,
                confirm=True,
                release=False,
            ),
            is_enabled=True,
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
        await database.close()


@pytest.mark.integration
async def test_repository_adds_provider_configuration() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name=f"provider-add-{provider_id.hex}",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://provider-add.test",
        request_timeout_ms=900,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )

    try:
        repository = SqlAlchemyProviderRepository(database)

        await repository.add(provider)

        assert await repository.get(provider_id) == provider
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
        await database.close()


@pytest.mark.integration
async def test_repository_maps_duplicate_provider_name_to_domain_conflict() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    provider_name = f"provider-duplicate-{uuid7().hex}"
    existing_provider = ProviderConfiguration(
        id=uuid7(),
        name=provider_name,
        kind=ProviderKind.INTERNAL,
        driver="internal",
        base_url=None,
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    duplicate_provider = ProviderConfiguration(
        id=uuid7(),
        name=provider_name,
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://duplicate-provider.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )

    try:
        repository = SqlAlchemyProviderRepository(database)
        await repository.add(existing_provider)

        with pytest.raises(ProviderNameConflictError):
            await repository.add(duplicate_provider)
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id.in_(
                        (existing_provider.id, duplicate_provider.id)
                    )
                )
            )
        await database.close()


@pytest.mark.integration
async def test_repository_updates_provider_configuration() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    provider_id = uuid7()
    original_provider = ProviderConfiguration(
        id=provider_id,
        name=f"provider-update-before-{provider_id.hex}",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://provider-update-before.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )
    updated_provider = ProviderConfiguration(
        id=provider_id,
        name=f"provider-update-after-{provider_id.hex}",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://provider-update-after.test",
        request_timeout_ms=2_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=False,
            confirm=False,
            release=False,
        ),
        is_enabled=False,
    )

    try:
        repository = SqlAlchemyProviderRepository(database)
        await repository.add(original_provider)

        await repository.update(updated_provider)

        assert await repository.get(provider_id) == updated_provider
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
        await database.close()


@pytest.mark.integration
async def test_repository_changes_provider_routing_status() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    provider_id = uuid7()
    provider = ProviderConfiguration(
        id=provider_id,
        name=f"provider-enable-{provider_id.hex}",
        kind=ProviderKind.EXTERNAL,
        driver="http",
        base_url="https://provider-enable.test",
        request_timeout_ms=1_000,
        capabilities=ProviderCapabilities(
            availability=True,
            hold=True,
            confirm=True,
            release=True,
        ),
        is_enabled=False,
    )

    try:
        repository = SqlAlchemyProviderRepository(database)
        await repository.add(provider)

        enabled_provider = await repository.set_enabled(
            provider_id,
            is_enabled=True,
        )

        assert enabled_provider == ProviderConfiguration(
            id=provider_id,
            name=f"provider-enable-{provider_id.hex}",
            kind=ProviderKind.EXTERNAL,
            driver="http",
            base_url="https://provider-enable.test",
            request_timeout_ms=1_000,
            capabilities=ProviderCapabilities(
                availability=True,
                hold=True,
                confirm=True,
                release=True,
            ),
            is_enabled=True,
        )
    finally:
        async with database.session() as session, session.begin():
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
        await database.close()
