from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_reservation.repository.models import (
    ReservationItemModel,
    ReservationModel,
)
from inventory_reservation.repository.models import (
    ReservationStatus as ReservationStatusModel,
)
from inventory_reservation.service.reservation import Reservation, ReservationItem


class SqlAlchemyReservationRepository:
    """Map reservation aggregates to PostgreSQL without owning the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, reservation: Reservation) -> None:
        reservation_model = ReservationModel(
            id=reservation.id,
            user_id=reservation.user_id,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            status=ReservationStatusModel(reservation.status.value),
            expires_at=reservation.expires_at,
            created_at=reservation.created_at,
        )
        reservation_model.items = [
            ReservationItemModel(
                reservation_id=reservation.id,
                product_id=item.product_id,
                requested_quantity=item.quantity,
            )
            for item in reservation.items
        ]
        self._session.add(reservation_model)

    async def get(self, reservation_id: UUID) -> Reservation | None:
        reservation_statement = select(ReservationModel).where(
            ReservationModel.id == reservation_id
        )
        reservation_model = (await self._session.scalars(reservation_statement)).one_or_none()
        return await self._to_domain(reservation_model)

    async def get_by_idempotency_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> Reservation | None:
        reservation_statement = select(ReservationModel).where(
            ReservationModel.user_id == user_id,
            ReservationModel.idempotency_key == idempotency_key,
        )
        reservation_model = (await self._session.scalars(reservation_statement)).one_or_none()
        return await self._to_domain(reservation_model)

    async def _to_domain(
        self,
        reservation_model: ReservationModel | None,
    ) -> Reservation | None:
        if reservation_model is None:
            return None

        items_statement = (
            select(ReservationItemModel)
            .where(ReservationItemModel.reservation_id == reservation_model.id)
            .order_by(ReservationItemModel.product_id)
        )
        item_models = (await self._session.scalars(items_statement)).all()

        return Reservation(
            id=reservation_model.id,
            user_id=reservation_model.user_id,
            items=tuple(
                ReservationItem(
                    product_id=item_model.product_id,
                    quantity=item_model.requested_quantity,
                )
                for item_model in item_models
            ),
            idempotency_key=reservation_model.idempotency_key,
            request_fingerprint=reservation_model.request_fingerprint,
            created_at=reservation_model.created_at,
            expires_at=reservation_model.expires_at,
        )
