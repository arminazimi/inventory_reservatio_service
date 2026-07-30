from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Response, status
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    generate_latest,
)
from pydantic import BaseModel

DatabaseReadinessCheck = Callable[[], Awaitable[bool]]


class LivenessResponse(BaseModel):
    status: str


class ReadinessChecks(BaseModel):
    database: Literal["up", "down"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


def create_operations_router(
    *,
    database_is_ready: DatabaseReadinessCheck | None = None,
    metrics_registry: CollectorRegistry | None = None,
) -> APIRouter:
    router = APIRouter(tags=["operations"])

    @router.get("/health/live")
    async def get_liveness() -> LivenessResponse:
        return LivenessResponse(status="ok")

    @router.get(
        "/health/ready",
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": ReadinessResponse,
                "description": "A required dependency is unavailable.",
            },
        },
    )
    async def get_readiness(response: Response) -> ReadinessResponse:
        database_ready = (
            await database_is_ready()
            if database_is_ready is not None
            else False
        )
        if database_ready:
            return ReadinessResponse(
                status="ready",
                checks=ReadinessChecks(database="up"),
            )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="not_ready",
            checks=ReadinessChecks(database="down"),
        )

    if metrics_registry is not None:

        @router.get("/metrics", include_in_schema=False)
        def get_metrics() -> Response:
            return Response(
                content=generate_latest(metrics_registry),
                headers={"Content-Type": CONTENT_TYPE_LATEST},
            )

    return router
