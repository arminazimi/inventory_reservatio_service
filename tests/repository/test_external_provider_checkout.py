import os
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import httpx
import pytest
from sqlalchemy import delete

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory import (
    InventoryRepository,
    InventorySnapshot,
)
from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    ProductModel,
    ProviderKind,
    ReservationModel,
)
from inventory_reservation.repository.provider import ProviderRegistry
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.service.reservation import (
    ReservationItem,
    ReservationService,
)

FIXED_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


@pytest.mark.integration
async def test_checkout_holds_external_provider_before_internal_fallback() -> None:
    database = Database(
        os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
        )
    )
    external_requests = 0

    async def external_provider_api(_: httpx.Request) -> httpx.Response:
        nonlocal external_requests
        external_requests += 1
        return httpx.Response(
            status_code=201,
            json={"hold_reference": "external-checkout-hold"},
        )

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(external_provider_api)
        ) as client:
            provider_registry = ProviderRegistry(client=client)

            async with database.session() as session, session.begin():
                unique_suffix = uuid7().hex
                product = ProductModel(
                    sku=f"EXTERNAL-CHECKOUT-{unique_suffix}",
                    name="External checkout test product",
                )
                external_provider = InventoryProviderModel(
                    name=f"external-checkout-{unique_suffix}",
                    kind=ProviderKind.EXTERNAL,
                    driver="http",
                    base_url="https://inventory-provider.example",
                    request_timeout_ms=2000,
                    supports_hold=True,
                )
                internal_provider = InventoryProviderModel(
                    name=f"internal-checkout-fallback-{unique_suffix}",
                    kind=ProviderKind.INTERNAL,
                    driver="internal",
                    supports_hold=True,
                )
                session.add_all([product, external_provider, internal_provider])
                await session.flush()
                product_id = product.id
                external_provider_id = external_provider.id
                internal_provider_id = internal_provider.id
                session.add_all(
                    [
                        InventoryLevelModel(
                            product_id=product_id,
                            provider_id=external_provider_id,
                            on_hand=0,
                            reserved=0,
                            allocation_priority=10,
                        ),
                        InventoryLevelModel(
                            product_id=product_id,
                            provider_id=internal_provider_id,
                            on_hand=5,
                            reserved=0,
                            allocation_priority=20,
                        ),
                    ]
                )

            reservation_id = uuid7()
            service = ReservationService(
                transaction_factory=lambda: reservation_transaction(
                    database,
                    provider_registry,
                ),
                clock=FixedClock(),
                reservation_id_factory=lambda: reservation_id,
                ttl=timedelta(minutes=15),
            )

            try:
                reservation = await service.create(
                    user_id=uuid7(),
                    items=(ReservationItem(product_id=product_id, quantity=2),),
                    idempotency_key="external-provider-checkout",
                )

                async with database.session() as session:
                    internal_inventory = await InventoryRepository(session).get_snapshot(
                        product_id=product_id,
                        provider_id=internal_provider_id,
                    )

                assert (
                    reservation.id,
                    external_requests,
                    internal_inventory,
                ) == (
                    reservation_id,
                    1,
                    InventorySnapshot(
                        product_id=product_id,
                        provider_id=internal_provider_id,
                        on_hand=5,
                        reserved=0,
                        version=1,
                    ),
                )
            finally:
                async with database.session() as session, session.begin():
                    await session.execute(
                        delete(ReservationModel).where(ReservationModel.id == reservation_id)
                    )
                    await session.execute(
                        delete(InventoryLevelModel).where(
                            InventoryLevelModel.product_id == product_id
                        )
                    )
                    await session.execute(
                        delete(InventoryProviderModel).where(
                            InventoryProviderModel.id.in_(
                                (external_provider_id, internal_provider_id)
                            )
                        )
                    )
                    await session.execute(delete(ProductModel).where(ProductModel.id == product_id))
    finally:
        await database.close()
