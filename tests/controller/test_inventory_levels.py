from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.inventory_management import (
    create_inventory_management_router,
    handle_inventory_below_reserved,
)
from inventory_reservation.controller.product import handle_product_not_found
from inventory_reservation.controller.provider import handle_provider_not_found
from inventory_reservation.service.inventory_management import (
    InventoryBelowReservedError,
    InventoryLevel,
    InventoryManagementService,
)
from inventory_reservation.service.product import ProductNotFoundError
from inventory_reservation.service.provider_management import (
    ProviderNotFoundError,
)

PRODUCT_ID = UUID("00000000-0000-7000-8000-000000000030")
PROVIDER_ID = UUID("00000000-0000-7000-8000-000000000031")


class InMemoryInventoryLevelRepository:
    def __init__(self) -> None:
        self.product_ids = {PRODUCT_ID}
        self.provider_ids = {PROVIDER_ID}
        self.levels: dict[tuple[UUID, UUID], InventoryLevel] = {}

    async def product_exists(self, product_id: UUID) -> bool:
        return product_id in self.product_ids

    async def provider_exists(self, provider_id: UUID) -> bool:
        return provider_id in self.provider_ids

    async def set_level(
        self,
        *,
        product_id: UUID,
        provider_id: UUID,
        on_hand: int,
        allocation_priority: int,
    ) -> InventoryLevel:
        key = (product_id, provider_id)
        current = self.levels.get(key)
        if current is None:
            level = InventoryLevel(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=on_hand,
                reserved=0,
                allocation_priority=allocation_priority,
                version=1,
            )
        elif (
            current.on_hand == on_hand
            and current.allocation_priority == allocation_priority
        ):
            level = current
        else:
            if on_hand < current.reserved:
                raise InventoryBelowReservedError(
                    product_id=product_id,
                    provider_id=provider_id,
                    reserved=current.reserved,
                )
            level = InventoryLevel(
                product_id=product_id,
                provider_id=provider_id,
                on_hand=on_hand,
                reserved=current.reserved,
                allocation_priority=allocation_priority,
                version=current.version + 1,
            )
        self.levels[key] = level
        return level


async def test_operator_can_assign_product_inventory_to_provider() -> None:
    service = InventoryManagementService(
        repository=InMemoryInventoryLevelRepository()
    )
    app = FastAPI()
    app.include_router(create_inventory_management_router(service))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            (
                f"/internal/v1/products/{PRODUCT_ID}"
                f"/providers/{PROVIDER_ID}/inventory"
            ),
            json={
                "on_hand": 12,
                "allocation_priority": 10,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "product_id": str(PRODUCT_ID),
        "provider_id": str(PROVIDER_ID),
        "on_hand": 12,
        "reserved": 0,
        "available": 12,
        "allocation_priority": 10,
        "version": 1,
    }


async def test_repeating_inventory_assignment_is_idempotent() -> None:
    service = InventoryManagementService(
        repository=InMemoryInventoryLevelRepository()
    )
    app = FastAPI()
    app.include_router(create_inventory_management_router(service))
    request_url = (
        f"/internal/v1/products/{PRODUCT_ID}"
        f"/providers/{PROVIDER_ID}/inventory"
    )
    request_body = {
        "on_hand": 12,
        "allocation_priority": 10,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_response = await client.put(
            request_url,
            json=request_body,
        )
        repeated_response = await client.put(
            request_url,
            json=request_body,
        )

    assert first_response.status_code == 200
    assert repeated_response.status_code == 200
    assert repeated_response.json() == first_response.json()


async def test_inventory_cannot_be_reduced_below_reserved_quantity() -> None:
    repository = InMemoryInventoryLevelRepository()
    repository.levels[(PRODUCT_ID, PROVIDER_ID)] = InventoryLevel(
        product_id=PRODUCT_ID,
        provider_id=PROVIDER_ID,
        on_hand=12,
        reserved=5,
        allocation_priority=10,
        version=2,
    )
    service = InventoryManagementService(repository=repository)
    app = FastAPI()
    app.include_router(create_inventory_management_router(service))
    app.add_exception_handler(
        InventoryBelowReservedError,
        handle_inventory_below_reserved,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            (
                f"/internal/v1/products/{PRODUCT_ID}"
                f"/providers/{PROVIDER_ID}/inventory"
            ),
            json={
                "on_hand": 4,
                "allocation_priority": 10,
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "inventory_below_reserved",
            "message": (
                "On-hand inventory cannot be lower than reserved inventory."
            ),
            "product_id": str(PRODUCT_ID),
            "provider_id": str(PROVIDER_ID),
            "reserved": 5,
        }
    }


async def test_assigning_unknown_product_returns_not_found() -> None:
    repository = InMemoryInventoryLevelRepository()
    repository.product_ids.clear()
    service = InventoryManagementService(repository=repository)
    app = FastAPI()
    app.include_router(create_inventory_management_router(service))
    app.add_exception_handler(
        ProductNotFoundError,
        handle_product_not_found,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            (
                f"/internal/v1/products/{PRODUCT_ID}"
                f"/providers/{PROVIDER_ID}/inventory"
            ),
            json={
                "on_hand": 12,
                "allocation_priority": 10,
            },
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "product_not_found",
            "message": "Product was not found.",
            "product_id": str(PRODUCT_ID),
        }
    }


async def test_assigning_unknown_provider_returns_not_found() -> None:
    repository = InMemoryInventoryLevelRepository()
    repository.provider_ids.clear()
    service = InventoryManagementService(repository=repository)
    app = FastAPI()
    app.include_router(create_inventory_management_router(service))
    app.add_exception_handler(
        ProviderNotFoundError,
        handle_provider_not_found,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            (
                f"/internal/v1/products/{PRODUCT_ID}"
                f"/providers/{PROVIDER_ID}/inventory"
            ),
            json={
                "on_hand": 12,
                "allocation_priority": 10,
            },
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "provider_not_found",
            "message": "Provider was not found.",
            "provider_id": str(PROVIDER_ID),
        }
    }
