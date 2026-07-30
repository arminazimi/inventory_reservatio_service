import httpx
from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.main import build_app


async def test_app_lifespan_closes_provider_http_client() -> None:
    provider_http_client = httpx.AsyncClient()
    app = build_app(provider_http_client=provider_http_client)

    async with app.router.lifespan_context(app):
        assert not provider_http_client.is_closed

    assert provider_http_client.is_closed


async def test_built_app_exposes_liveness_endpoint() -> None:
    app = build_app()

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get("/health/live")

    assert response.status_code == 200


async def test_built_app_exposes_prometheus_api_metrics() -> None:
    app = build_app()

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        await client.get("/health/live")
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "inventory_reservation_http_requests_total" in response.text
