from httpx import ASGITransport, AsyncClient

from inventory_reservation.controller.external_provider_mock import (
    create_external_provider_mock_app,
)


async def test_mock_exposes_deterministic_external_provider_scenarios() -> None:
    app = create_external_provider_mock_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://provider-mock",
    ) as client:
        successful_hold = await client.post(
            "/scenarios/success/holds",
            json={"product_id": "00000000-0000-7000-8000-000000000001", "quantity": 2},
        )
        failed_hold = await client.post(
            "/scenarios/server-error/holds",
            json={"product_id": "00000000-0000-7000-8000-000000000001", "quantity": 2},
        )
        stale_availability = await client.get(
            "/scenarios/stale/availability/00000000-0000-7000-8000-000000000001"
        )

    assert successful_hold.status_code == 201
    assert successful_hold.json()["hold_reference"].startswith("success-")
    assert failed_hold.status_code == 503
    assert stale_availability.status_code == 200
    assert stale_availability.json()["available_quantity"] == 0
