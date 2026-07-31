"""Deterministic external-provider simulator for reviewer-facing scenarios."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from fastapi import FastAPI, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse


class HoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    quantity: int = Field(gt=0)


def create_external_provider_mock_app() -> FastAPI:
    app = FastAPI(
        title="Inventory Reservation External Provider Mock",
        version="0.1.0",
    )
    calls: Counter[str] = Counter()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.delete("/admin/calls", status_code=status.HTTP_204_NO_CONTENT)
    async def reset_calls() -> Response:
        calls.clear()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/admin/calls")
    async def get_calls() -> dict[str, int]:
        return dict(calls)

    @app.get("/scenarios/{scenario}/availability/{product_id}")
    async def availability(
        scenario: str,
        product_id: UUID,
    ) -> JSONResponse:
        calls[f"{scenario}:availability"] += 1
        if scenario == "unexpected":
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"unexpected": "payload"},
            )
        observed_at = datetime.now(UTC)
        if scenario == "stale":
            observed_at -= timedelta(minutes=5)
        available_quantity = 0 if scenario in {"out-of-stock", "stale"} else 10
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "available_quantity": available_quantity,
                "observed_at": observed_at.isoformat(),
            },
        )

    @app.post("/scenarios/{scenario}/holds", status_code=status.HTTP_201_CREATED)
    async def hold(scenario: str, request: HoldRequest) -> JSONResponse:
        calls[f"{scenario}:hold"] += 1
        if scenario == "server-error":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"code": "provider_unavailable"},
            )
        if scenario == "out-of-stock":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"code": "out_of_stock"},
            )
        if scenario == "hold-timeout":
            await asyncio.sleep(2)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "hold_reference": f"{scenario}-{request.product_id}-{uuid7()}"
            },
        )

    @app.post("/scenarios/{scenario}/holds/{hold_reference}/confirm")
    async def confirm(scenario: str, hold_reference: str) -> Response:
        calls[f"{scenario}:confirm"] += 1
        if scenario == "confirm-timeout":
            await asyncio.sleep(2)
        if scenario == "confirm-error":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"code": "confirmation_unavailable"},
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/scenarios/{scenario}/holds/{hold_reference}/release")
    async def release(scenario: str, hold_reference: str) -> Response:
        calls[f"{scenario}:release"] += 1
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_external_provider_mock_app()
