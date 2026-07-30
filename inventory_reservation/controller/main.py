import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import httpx
from fastapi import FastAPI
from starlette.types import Lifespan

from inventory_reservation.controller.inventory_management import (
    create_inventory_management_router,
    handle_inventory_below_reserved,
)
from inventory_reservation.controller.product import (
    create_product_router,
    handle_invalid_product_configuration,
    handle_product_not_found,
    handle_product_sku_conflict,
)
from inventory_reservation.controller.provider import (
    create_provider_router,
    handle_invalid_provider_configuration,
    handle_provider_name_conflict,
    handle_provider_not_found,
)
from inventory_reservation.controller.reservation import (
    ReservationNotFoundError,
    create_reservation_router,
    handle_idempotency_conflict,
    handle_insufficient_inventory,
    handle_reconciliation_required,
    handle_reservation_not_cancellable,
    handle_reservation_not_confirmable,
    handle_reservation_not_found,
)
from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory_management import (
    SqlAlchemyInventoryLevelRepository,
)
from inventory_reservation.repository.product import (
    SqlAlchemyProductRepository,
)
from inventory_reservation.repository.provider import (
    EnvironmentSecretResolver,
    ProviderRegistry,
)
from inventory_reservation.repository.provider_management import (
    SqlAlchemyProviderRepository,
)
from inventory_reservation.repository.reservation import reservation_transaction
from inventory_reservation.service.inventory_management import (
    InventoryBelowReservedError,
    InventoryManagementService,
)
from inventory_reservation.service.product import (
    InvalidProductConfigurationError,
    ProductManagementService,
    ProductNotFoundError,
    ProductSkuConflictError,
)
from inventory_reservation.service.provider_management import (
    InvalidProviderConfigurationError,
    ProviderManagementService,
    ProviderNameConflictError,
    ProviderNotFoundError,
)
from inventory_reservation.service.reservation import (
    IdempotencyConflictError,
    InsufficientInventoryError,
    ReservationNotCancellableError,
    ReservationNotConfirmableError,
    ReservationReconciliationRequiredError,
    ReservationService,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory"
DEFAULT_RESERVATION_TTL_SECONDS = 900
DEFAULT_PROVIDER_FAILURE_THRESHOLD = 3
DEFAULT_PROVIDER_RECOVERY_TIMEOUT_SECONDS = 30.0


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def create_app(
    *,
    reservation_service: ReservationService,
    inventory_management_service: InventoryManagementService | None = None,
    provider_management_service: ProviderManagementService | None = None,
    product_management_service: ProductManagementService | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Inventory Reservation Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(create_reservation_router(reservation_service))
    if inventory_management_service is not None:
        app.include_router(
            create_inventory_management_router(
                inventory_management_service
            )
        )
    if provider_management_service is not None:
        app.include_router(
            create_provider_router(provider_management_service)
        )
    if product_management_service is not None:
        app.include_router(
            create_product_router(product_management_service)
        )
    app.add_exception_handler(
        InventoryBelowReservedError,
        handle_inventory_below_reserved,
    )
    app.add_exception_handler(
        InvalidProductConfigurationError,
        handle_invalid_product_configuration,
    )
    app.add_exception_handler(
        ProductSkuConflictError,
        handle_product_sku_conflict,
    )
    app.add_exception_handler(
        ProductNotFoundError,
        handle_product_not_found,
    )
    app.add_exception_handler(
        ProviderNotFoundError,
        handle_provider_not_found,
    )
    app.add_exception_handler(
        InvalidProviderConfigurationError,
        handle_invalid_provider_configuration,
    )
    app.add_exception_handler(
        ProviderNameConflictError,
        handle_provider_name_conflict,
    )
    app.add_exception_handler(
        IdempotencyConflictError,
        handle_idempotency_conflict,
    )
    app.add_exception_handler(
        InsufficientInventoryError,
        handle_insufficient_inventory,
    )
    app.add_exception_handler(
        ReservationNotFoundError,
        handle_reservation_not_found,
    )
    app.add_exception_handler(
        ReservationNotCancellableError,
        handle_reservation_not_cancellable,
    )
    app.add_exception_handler(
        ReservationNotConfirmableError,
        handle_reservation_not_confirmable,
    )
    app.add_exception_handler(
        ReservationReconciliationRequiredError,
        handle_reconciliation_required,
    )
    return app


def build_app(
    *,
    provider_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    database = Database(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    http_client = provider_http_client if provider_http_client is not None else httpx.AsyncClient()
    provider_registry = ProviderRegistry(
        client=http_client,
        secret_resolver=EnvironmentSecretResolver(),
        failure_threshold=int(
            os.getenv(
                "PROVIDER_FAILURE_THRESHOLD",
                str(DEFAULT_PROVIDER_FAILURE_THRESHOLD),
            )
        ),
        recovery_timeout=float(
            os.getenv(
                "PROVIDER_RECOVERY_TIMEOUT_SECONDS",
                str(DEFAULT_PROVIDER_RECOVERY_TIMEOUT_SECONDS),
            )
        ),
    )
    reservation_service = ReservationService(
        transaction_factory=lambda: reservation_transaction(
            database,
            provider_registry,
        ),
        clock=UtcClock(),
        reservation_id_factory=uuid7,
        ttl=timedelta(
            seconds=int(
                os.getenv(
                    "RESERVATION_TTL_SECONDS",
                    str(DEFAULT_RESERVATION_TTL_SECONDS),
                )
            )
        ),
    )
    provider_management_service = ProviderManagementService(
        repository=SqlAlchemyProviderRepository(database),
    )
    inventory_management_service = InventoryManagementService(
        repository=SqlAlchemyInventoryLevelRepository(database),
    )
    product_management_service = ProductManagementService(
        repository=SqlAlchemyProductRepository(database),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                await http_client.aclose()
            finally:
                await database.close()

    return create_app(
        reservation_service=reservation_service,
        inventory_management_service=inventory_management_service,
        provider_management_service=provider_management_service,
        product_management_service=product_management_service,
        lifespan=lifespan,
    )


app = build_app()
