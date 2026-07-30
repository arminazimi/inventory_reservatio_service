import os
from uuid import UUID, uuid7

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from inventory_reservation.controller.main import build_app
from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import (
    InventoryLevelModel,
    InventoryProviderModel,
    OrderModel,
    ProductModel,
    ReservationModel,
)


async def cleanup_checkout_data(
    database: Database,
    *,
    user_id: UUID,
    product_id: UUID | None,
    provider_id: UUID | None,
) -> None:
    async with database.session() as session, session.begin():
        await session.execute(
            delete(OrderModel).where(OrderModel.user_id == user_id)
        )
        await session.execute(
            delete(ReservationModel).where(
                ReservationModel.user_id == user_id
            )
        )
        if product_id is not None:
            await session.execute(
                delete(InventoryLevelModel).where(
                    InventoryLevelModel.product_id == product_id
                )
            )
        if provider_id is not None:
            await session.execute(
                delete(InventoryProviderModel).where(
                    InventoryProviderModel.id == provider_id
                )
            )
        if product_id is not None:
            await session.execute(
                delete(ProductModel).where(
                    ProductModel.id == product_id
                )
            )


@pytest.mark.integration
async def test_user_can_complete_checkout_against_internal_inventory() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory",
    )
    cleanup_database = Database(database_url)
    app = build_app()
    unique_suffix = uuid7().hex
    user_id = uuid7()
    product_id: UUID | None = None
    provider_id: UUID | None = None

    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client,
        ):
            product_response = await client.post(
                "/internal/v1/products",
                json={
                    "sku": f"E2E-CHECKOUT-{unique_suffix}",
                    "name": "E2E checkout product",
                },
            )
            assert product_response.status_code == 201
            product_id = UUID(product_response.json()["id"])

            provider_response = await client.post(
                "/internal/v1/providers",
                json={
                    "name": f"e2e-internal-{unique_suffix}",
                    "kind": "internal",
                    "driver": "internal",
                    "request_timeout_ms": 500,
                    "capabilities": {
                        "availability": True,
                        "hold": True,
                        "confirm": True,
                        "release": True,
                    },
                },
            )
            assert provider_response.status_code == 201
            provider_id = UUID(provider_response.json()["id"])

            enable_response = await client.post(
                f"/internal/v1/providers/{provider_id}/enable"
            )
            assert enable_response.status_code == 200

            inventory_url = (
                f"/internal/v1/products/{product_id}"
                f"/providers/{provider_id}/inventory"
            )
            inventory_response = await client.put(
                inventory_url,
                json={
                    "on_hand": 5,
                    "allocation_priority": 10,
                },
            )
            assert inventory_response.status_code == 200

            reservation_headers = {
                "X-User-ID": str(user_id),
                "Idempotency-Key": f"e2e-checkout-{unique_suffix}",
            }
            reservation_payload = {
                "items": [
                    {
                        "product_id": str(product_id),
                        "quantity": 2,
                    }
                ]
            }
            reservation_response = await client.post(
                "/v1/reservations",
                headers=reservation_headers,
                json=reservation_payload,
            )
            assert reservation_response.status_code == 201
            reservation = reservation_response.json()
            assert reservation["status"] == "pending"

            retried_reservation_response = await client.post(
                "/v1/reservations",
                headers=reservation_headers,
                json=reservation_payload,
            )
            assert retried_reservation_response.status_code == 201
            assert retried_reservation_response.json() == reservation

            confirmation_url = (
                f"/v1/reservations/{reservation['id']}/confirm"
            )
            confirmation_response = await client.post(
                confirmation_url,
                headers={"X-User-ID": str(user_id)},
            )
            assert confirmation_response.status_code == 200
            confirmation = confirmation_response.json()
            assert confirmation["status"] == "confirmed"

            retried_confirmation_response = await client.post(
                confirmation_url,
                headers={"X-User-ID": str(user_id)},
            )
            assert retried_confirmation_response.status_code == 200
            assert retried_confirmation_response.json() == confirmation

            final_inventory_response = await client.get(inventory_url)
            assert final_inventory_response.status_code == 200
            final_inventory = final_inventory_response.json()
            assert (
                final_inventory["on_hand"],
                final_inventory["reserved"],
                final_inventory["available"],
            ) == (3, 0, 3)
    finally:
        await cleanup_checkout_data(
            cleanup_database,
            user_id=user_id,
            product_id=product_id,
            provider_id=provider_id,
        )
        await cleanup_database.close()
