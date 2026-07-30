from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry

from inventory_reservation.controller.operations import create_operations_router
from inventory_reservation.repository.telemetry import PrometheusHttpMetrics


async def test_liveness_reports_running_process() -> None:
    app = FastAPI()
    app.include_router(create_operations_router())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_available_database() -> None:
    async def database_is_ready() -> bool:
        return True

    app = FastAPI()
    app.include_router(
        create_operations_router(database_is_ready=database_is_ready)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "up"},
    }


async def test_readiness_rejects_traffic_when_database_is_unavailable() -> None:
    async def database_is_ready() -> bool:
        return False

    app = FastAPI()
    app.include_router(
        create_operations_router(database_is_ready=database_is_ready)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "down"},
    }


async def test_metrics_report_api_requests_by_route_and_status() -> None:
    registry = CollectorRegistry()
    app = FastAPI()
    app.add_middleware(
        PrometheusHttpMetrics,
        registry=registry,
    )
    app.include_router(
        create_operations_router(metrics_registry=registry)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/health/live")
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert (
        'inventory_reservation_http_requests_total'
        '{method="GET",route="/health/live",status="200"} 1.0'
        in response.text
    )
    assert "inventory_reservation_http_request_duration_seconds" in response.text
    assert (
        'inventory_reservation_http_requests_in_progress{method="GET"} 0.0'
        in response.text
    )


async def test_metrics_use_route_templates_instead_of_resource_ids() -> None:
    registry = CollectorRegistry()
    app = FastAPI()
    app.add_middleware(
        PrometheusHttpMetrics,
        registry=registry,
    )

    @app.get("/widgets/{widget_id}")
    async def get_widget(widget_id: int) -> dict[str, int]:
        return {"widget_id": widget_id}

    app.include_router(
        create_operations_router(metrics_registry=registry)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/widgets/42")
        response = await client.get("/metrics")

    assert (
        'inventory_reservation_http_requests_total'
        '{method="GET",route="/widgets/{widget_id}",status="200"} 1.0'
        in response.text
    )
    assert 'route="/widgets/42"' not in response.text
