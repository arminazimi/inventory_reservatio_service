from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from inventory_reservation.service.reservation import (
    IdempotencyConflictError,
    Reservation,
    ReservationItem,
    ReservationService,
    ReservationStatus,
)


class ReservationItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    quantity: int = Field(gt=0)


class CreateReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReservationItemRequest] = Field(min_length=1)


class ReservationItemResponse(BaseModel):
    product_id: UUID
    quantity: int


class ReservationResponse(BaseModel):
    id: UUID
    user_id: UUID
    status: ReservationStatus
    items: list[ReservationItemResponse]
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_domain(cls, reservation: Reservation) -> ReservationResponse:
        return cls(
            id=reservation.id,
            user_id=reservation.user_id,
            status=reservation.status,
            items=[
                ReservationItemResponse(
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                for item in reservation.items
            ],
            created_at=reservation.created_at,
            expires_at=reservation.expires_at,
        )


class ErrorDetail(BaseModel):
    code: str
    message: str
    reservation_id: UUID


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ReservationNotFoundError(LookupError):
    def __init__(self, reservation_id: UUID) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"Reservation {reservation_id} was not found")


async def handle_idempotency_conflict(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, IdempotencyConflictError):
        raise error

    response = ErrorResponse(
        error=ErrorDetail(
            code="idempotency_conflict",
            message="Idempotency key is already associated with a different request.",
            reservation_id=error.reservation_id,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=response.model_dump(mode="json"),
    )


async def handle_reservation_not_found(
    _: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, ReservationNotFoundError):
        raise error

    response = ErrorResponse(
        error=ErrorDetail(
            code="reservation_not_found",
            message="Reservation was not found.",
            reservation_id=error.reservation_id,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=response.model_dump(mode="json"),
    )


def create_reservation_router(reservation_service: ReservationService) -> APIRouter:
    router = APIRouter(prefix="/v1/reservations", tags=["reservations"])

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_409_CONFLICT: {
                "model": ErrorResponse,
                "description": "Idempotency key was reused for a different request.",
            }
        },
    )
    async def create_reservation(
        request: CreateReservationRequest,
        user_id: Annotated[UUID, Header(alias="X-User-ID")],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> ReservationResponse:
        reservation = await reservation_service.create(
            user_id=user_id,
            items=tuple(
                ReservationItem(
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                for item in request.items
            ),
            idempotency_key=idempotency_key,
        )
        return ReservationResponse.from_domain(reservation)

    @router.get(
        "/{reservation_id}",
        responses={
            status.HTTP_404_NOT_FOUND: {
                "model": ErrorResponse,
                "description": "Reservation does not exist or belongs to another user.",
            }
        },
    )
    async def get_reservation(
        reservation_id: Annotated[UUID, Path(description="Reservation identifier")],
        user_id: Annotated[UUID, Header(alias="X-User-ID")],
    ) -> ReservationResponse:
        reservation = await reservation_service.get(reservation_id)
        if reservation is None or reservation.user_id != user_id:
            raise ReservationNotFoundError(reservation_id)
        return ReservationResponse.from_domain(reservation)

    return router
