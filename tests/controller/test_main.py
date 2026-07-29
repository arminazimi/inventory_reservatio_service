import httpx

from inventory_reservation.controller.main import build_app


async def test_app_lifespan_closes_provider_http_client() -> None:
    provider_http_client = httpx.AsyncClient()
    app = build_app(provider_http_client=provider_http_client)

    async with app.router.lifespan_context(app):
        assert not provider_http_client.is_closed

    assert provider_http_client.is_closed
