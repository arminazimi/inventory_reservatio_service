from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.inventory import InventoryRepository
from inventory_reservation.repository.models import (
    AllocationStatus,
    InventoryAllocationModel,
    InventoryProviderModel,
    OrderItemModel,
    OrderModel,
    ProviderCredentialModel,
    ProviderKind,
    ProviderOperationModel,
    ProviderOperationStatus,
    ProviderOperationType,
    ReservationItemModel,
    ReservationModel,
)
from inventory_reservation.repository.models import (
    ReservationStatus as ReservationStatusModel,
)
from inventory_reservation.repository.provider import ProviderRegistry
from inventory_reservation.service.provider import (
    ConfirmCommand,
    ProviderConfirmOutcome,
    ProviderReleaseOutcome,
    ReleaseCommand,
)
from inventory_reservation.service.provider_management import (
    ProviderAuthType,
    ProviderCredentialConfiguration,
)
from inventory_reservation.service.reservation import (
    ConcurrentReservationCreationError,
    Reservation,
    ReservationItem,
    ReservationNotCancellableError,
    ReservationNotConfirmableError,
    ReservationReconciliationRequiredError,
    ReservationRepositoryPort,
    ReservationStatus,
)

IDEMPOTENCY_CONSTRAINT = "uq_reservations_user_id_idempotency_key"


def _to_provider_credentials(
    credentials: ProviderCredentialModel | None,
) -> ProviderCredentialConfiguration | None:
    if credentials is None:
        return None
    return ProviderCredentialConfiguration(
        provider_id=credentials.provider_id,
        auth_type=ProviderAuthType(credentials.auth_type.value),
        secret_ref=credentials.secret_ref,
        public_config=credentials.public_config,
    )


class _InventoryConfirmationError(RuntimeError):
    pass


class _InventoryReleaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ProviderOperationContext:
    reservation_id: UUID
    retry_unknown: bool
    max_attempts: int
    retry_started_at: datetime | None = None
    retry_base_delay: timedelta | None = None

    def next_attempt_at(self, attempt_count: int) -> datetime | None:
        if self.retry_started_at is None or self.retry_base_delay is None:
            return None
        retry_number = max(0, attempt_count - 2)
        delay_seconds = self.retry_base_delay.total_seconds() * (2**retry_number)
        return self.retry_started_at + timedelta(seconds=delay_seconds)


@dataclass(frozen=True, slots=True)
class _ConfiguredProvider:
    provider: InventoryProviderModel
    credentials: ProviderCredentialModel | None


class SqlAlchemyReservationRepository:
    """Map reservation aggregates to PostgreSQL without owning the transaction."""

    def __init__(
        self,
        session: AsyncSession,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        self._session = session
        self._provider_registry = provider_registry

    async def add(self, reservation: Reservation) -> None:
        self._session.add(self._to_model(reservation))

    async def add_with_hold(self, reservation: Reservation) -> bool:
        reservation_model = self._to_model(reservation)
        self._session.add(reservation_model)
        await self._session.flush()

        inventory_repository = InventoryRepository(
            self._session,
            self._provider_registry,
        )
        for item_model in reservation_model.items:
            hold_idempotency_key = (
                f"reservation:{reservation.id}:product:{item_model.product_id}:"
                f"provider:{item_model.provider_id}:hold"
            )
            hold = await inventory_repository.try_hold_selected(
                product_id=item_model.product_id,
                provider_id=item_model.provider_id,
                quantity=item_model.requested_quantity,
                idempotency_key=hold_idempotency_key,
            )
            if hold is None:
                reservation_model.failure_code = "insufficient_inventory"
                reservation_model.failure_reason = (
                    "A later reservation item could not be allocated."
                )
                await self._release_reservation(
                    reservation_model,
                    terminal_status=ReservationStatusModel.FAILED,
                )
                await self._session.flush()
                return False

            self._session.add(
                InventoryAllocationModel(
                    reservation_item_id=item_model.id,
                    provider_id=hold.provider_id,
                    quantity=item_model.requested_quantity,
                    status=AllocationStatus.HELD,
                    hold_idempotency_key=hold_idempotency_key,
                    provider_hold_reference=hold.reference,
                )
            )

        return True

    async def confirm(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None:
        reservation_statement = (
            select(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.user_id == user_id,
            )
            .with_for_update()
        )
        reservation_model = (await self._session.scalars(reservation_statement)).one_or_none()
        if reservation_model is None:
            return None
        if reservation_model.status is ReservationStatusModel.CONFIRMED:
            await self._ensure_order(reservation_model)
            return await self._to_domain(reservation_model)
        if reservation_model.status is ReservationStatusModel.CANCELLED:
            raise ReservationNotConfirmableError(reservation_id)

        return await self._confirm_reservation(
            reservation_model,
            context=_ProviderOperationContext(
                reservation_id=reservation_model.id,
                retry_unknown=False,
                max_attempts=1,
            ),
        )

    async def _confirm_reservation(
        self,
        reservation: ReservationModel,
        *,
        context: _ProviderOperationContext,
    ) -> Reservation | None:
        allocations_statement = (
            select(
                InventoryAllocationModel,
                ReservationItemModel.product_id,
                InventoryProviderModel,
                ProviderCredentialModel,
            )
            .join(
                ReservationItemModel,
                ReservationItemModel.id == InventoryAllocationModel.reservation_item_id,
            )
            .join(
                InventoryProviderModel,
                InventoryProviderModel.id == InventoryAllocationModel.provider_id,
            )
            .outerjoin(
                ProviderCredentialModel,
                ProviderCredentialModel.provider_id == InventoryProviderModel.id,
            )
            .where(ReservationItemModel.reservation_id == reservation.id)
            .with_for_update(of=InventoryAllocationModel)
        )
        allocations = (await self._session.execute(allocations_statement)).all()
        inventory_repository = InventoryRepository(
            self._session,
            self._provider_registry,
        )
        confirmation_pending = False
        confirmation_failed = False

        for allocation, product_id, provider, credentials in allocations:
            outcome = await self._confirm_allocation(
                context=context,
                allocation=allocation,
                product_id=product_id,
                configured_provider=_ConfiguredProvider(
                    provider=provider,
                    credentials=credentials,
                ),
                inventory_repository=inventory_repository,
            )
            if outcome is ProviderConfirmOutcome.CONFIRMED:
                allocation.status = AllocationStatus.CONFIRMED
            elif outcome is ProviderConfirmOutcome.REJECTED:
                allocation.status = AllocationStatus.FAILED
                confirmation_failed = True
            elif outcome is ProviderConfirmOutcome.UNKNOWN:
                allocation.status = AllocationStatus.UNKNOWN
                confirmation_pending = True
            else:
                allocation.status = AllocationStatus.HELD
                confirmation_pending = True

        if confirmation_failed:
            reservation.status = ReservationStatusModel.FAILED
        elif confirmation_pending:
            reservation.status = ReservationStatusModel.CONFIRMING
        else:
            reservation.status = ReservationStatusModel.CONFIRMED
            await self._ensure_order(reservation)
        await self._session.flush()
        return await self._to_domain(reservation)

    async def cancel(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
    ) -> Reservation | None:
        reservation_statement = (
            select(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.user_id == user_id,
            )
            .with_for_update()
        )
        reservation_model = (await self._session.scalars(reservation_statement)).one_or_none()
        if reservation_model is None:
            return None
        if reservation_model.status is ReservationStatusModel.CANCELLED:
            return await self._to_domain(reservation_model)
        if reservation_model.status is ReservationStatusModel.CONFIRMED:
            raise ReservationNotCancellableError(reservation_id)
        if reservation_model.status is ReservationStatusModel.CONFIRMING:
            raise ReservationReconciliationRequiredError(reservation_id)

        await self._release_reservation(
            reservation_model,
            terminal_status=ReservationStatusModel.CANCELLED,
        )
        await self._session.flush()
        return await self._to_domain(reservation_model)

    async def expire_batch(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Reservation, ...]:
        expired_statement = (
            select(ReservationModel)
            .where(
                ReservationModel.status == ReservationStatusModel.PENDING,
                ReservationModel.expires_at <= now,
            )
            .order_by(ReservationModel.expires_at, ReservationModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        reservations = (await self._session.scalars(expired_statement)).all()
        for reservation in reservations:
            await self._release_reservation(
                reservation,
                terminal_status=ReservationStatusModel.EXPIRED,
            )

        await self._session.flush()
        expired: list[Reservation] = []
        for reservation in reservations:
            reservation_domain = await self._to_domain(reservation)
            if reservation_domain is not None:
                expired.append(reservation_domain)
        return tuple(expired)

    async def reconcile_batch(
        self,
        *,
        now: datetime,
        limit: int,
        max_attempts: int,
        retry_base_delay: timedelta,
    ) -> tuple[Reservation, ...]:
        has_reconcilable_operation = (
            select(ProviderOperationModel.id)
            .join(
                InventoryAllocationModel,
                InventoryAllocationModel.id == ProviderOperationModel.allocation_id,
            )
            .join(
                ReservationItemModel,
                ReservationItemModel.id == InventoryAllocationModel.reservation_item_id,
            )
            .where(
                ReservationItemModel.reservation_id == ReservationModel.id,
                ProviderOperationModel.status == ProviderOperationStatus.UNKNOWN,
                ProviderOperationModel.attempt_count < max_attempts,
                or_(
                    ProviderOperationModel.next_attempt_at.is_(None),
                    ProviderOperationModel.next_attempt_at <= now,
                ),
                or_(
                    and_(
                        ReservationModel.status == ReservationStatusModel.RELEASING,
                        ProviderOperationModel.operation == ProviderOperationType.RELEASE,
                    ),
                    and_(
                        ReservationModel.status == ReservationStatusModel.CONFIRMING,
                        ProviderOperationModel.operation == ProviderOperationType.CONFIRM,
                    ),
                ),
            )
            .exists()
        )
        statement = (
            select(ReservationModel)
            .where(
                or_(
                    and_(
                        ReservationModel.status == ReservationStatusModel.RELEASING,
                        ReservationModel.release_target_status.in_(
                            (
                                ReservationStatusModel.CANCELLED,
                                ReservationStatusModel.EXPIRED,
                                ReservationStatusModel.FAILED,
                            )
                        ),
                    ),
                    ReservationModel.status == ReservationStatusModel.CONFIRMING,
                ),
                has_reconcilable_operation,
            )
            .order_by(ReservationModel.updated_at, ReservationModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        reservations = (await self._session.scalars(statement)).all()
        for reservation in reservations:
            context = _ProviderOperationContext(
                reservation_id=reservation.id,
                retry_unknown=True,
                max_attempts=max_attempts,
                retry_started_at=now,
                retry_base_delay=retry_base_delay,
            )
            if reservation.status is ReservationStatusModel.RELEASING:
                terminal_status = reservation.release_target_status
                if terminal_status is None:
                    continue
                await self._release_reservation(
                    reservation,
                    terminal_status=terminal_status,
                    context=context,
                )
            elif reservation.status is ReservationStatusModel.CONFIRMING:
                await self._confirm_reservation(
                    reservation,
                    context=context,
                )

        await self._session.flush()
        reconciled: list[Reservation] = []
        for reservation in reservations:
            reservation_domain = await self._to_domain(reservation)
            if reservation_domain is not None:
                reconciled.append(reservation_domain)
        return tuple(reconciled)

    async def _release_reservation(
        self,
        reservation: ReservationModel,
        *,
        terminal_status: ReservationStatusModel,
        context: _ProviderOperationContext | None = None,
    ) -> None:
        allocations_statement = (
            select(
                InventoryAllocationModel,
                ReservationItemModel.product_id,
                InventoryProviderModel,
                ProviderCredentialModel,
            )
            .join(
                ReservationItemModel,
                ReservationItemModel.id == InventoryAllocationModel.reservation_item_id,
            )
            .join(
                InventoryProviderModel,
                InventoryProviderModel.id == InventoryAllocationModel.provider_id,
            )
            .outerjoin(
                ProviderCredentialModel,
                ProviderCredentialModel.provider_id == InventoryProviderModel.id,
            )
            .where(ReservationItemModel.reservation_id == reservation.id)
            .with_for_update(of=InventoryAllocationModel)
        )
        allocations = (await self._session.execute(allocations_statement)).all()
        inventory_repository = InventoryRepository(
            self._session,
            self._provider_registry,
        )
        release_pending = False
        release_context = context or _ProviderOperationContext(
            reservation_id=reservation.id,
            retry_unknown=False,
            max_attempts=1,
        )
        reservation.release_target_status = terminal_status

        for allocation, product_id, provider, credentials in allocations:
            outcome = await self._release_allocation(
                context=release_context,
                allocation=allocation,
                product_id=product_id,
                configured_provider=_ConfiguredProvider(
                    provider=provider,
                    credentials=credentials,
                ),
                inventory_repository=inventory_repository,
            )
            if outcome is ProviderReleaseOutcome.RELEASED:
                allocation.status = AllocationStatus.RELEASED
            elif outcome is ProviderReleaseOutcome.UNKNOWN:
                allocation.status = AllocationStatus.UNKNOWN
                release_pending = True
            else:
                allocation.status = AllocationStatus.HELD
                release_pending = True

        if release_pending:
            reservation.status = ReservationStatusModel.RELEASING
        else:
            reservation.status = terminal_status
            reservation.release_target_status = None

    async def _release_allocation(
        self,
        *,
        context: _ProviderOperationContext,
        allocation: InventoryAllocationModel,
        product_id: UUID,
        configured_provider: _ConfiguredProvider,
        inventory_repository: InventoryRepository,
    ) -> ProviderReleaseOutcome:
        provider = configured_provider.provider
        if allocation.status is AllocationStatus.RELEASED:
            return ProviderReleaseOutcome.RELEASED
        if provider.kind is ProviderKind.EXTERNAL:
            return await self._release_external_hold(
                context=context,
                allocation=allocation,
                provider=provider,
                credentials=configured_provider.credentials,
            )

        released_inventory = await inventory_repository.release_hold(
            product_id=product_id,
            provider_id=allocation.provider_id,
            quantity=allocation.quantity,
        )
        if released_inventory is None:
            raise _InventoryReleaseError
        return ProviderReleaseOutcome.RELEASED

    async def _release_external_hold(
        self,
        *,
        context: _ProviderOperationContext,
        allocation: InventoryAllocationModel,
        provider: InventoryProviderModel,
        credentials: ProviderCredentialModel | None,
    ) -> ProviderReleaseOutcome:
        if (
            self._provider_registry is None
            or not provider.supports_release
            or provider.base_url is None
            or provider.driver != "http"
            or allocation.provider_hold_reference is None
        ):
            raise _InventoryReleaseError

        idempotency_key = f"reservation:{context.reservation_id}:allocation:{allocation.id}:release"
        operation_statement = (
            select(ProviderOperationModel)
            .where(ProviderOperationModel.idempotency_key == idempotency_key)
            .with_for_update()
        )
        operation = (await self._session.scalars(operation_statement)).one_or_none()
        if operation is not None:
            if operation.status is ProviderOperationStatus.SUCCEEDED:
                return ProviderReleaseOutcome.RELEASED
            if operation.status is ProviderOperationStatus.UNKNOWN and (
                not context.retry_unknown or operation.attempt_count >= context.max_attempts
            ):
                return ProviderReleaseOutcome.UNKNOWN
            operation.status = ProviderOperationStatus.IN_PROGRESS
            operation.attempt_count += 1
            operation.next_attempt_at = None
            operation.error_code = None
            operation.error_message = None
        else:
            operation = ProviderOperationModel(
                allocation_id=allocation.id,
                operation=ProviderOperationType.RELEASE,
                status=ProviderOperationStatus.IN_PROGRESS,
                idempotency_key=idempotency_key,
                attempt_count=1,
                external_reference=allocation.provider_hold_reference,
            )
            self._session.add(operation)
        await self._session.flush()

        external_provider = self._provider_registry.get_external(
            provider_id=provider.id,
            base_url=provider.base_url,
            timeout=provider.request_timeout_ms / 1000,
            credentials=_to_provider_credentials(credentials),
        )
        attempt = await external_provider.release(
            ReleaseCommand(
                hold_reference=allocation.provider_hold_reference,
                idempotency_key=idempotency_key,
            )
        )
        if attempt.outcome is ProviderReleaseOutcome.RELEASED:
            operation.status = ProviderOperationStatus.SUCCEEDED
            operation.next_attempt_at = None
        elif attempt.outcome is ProviderReleaseOutcome.UNKNOWN:
            operation.status = ProviderOperationStatus.UNKNOWN
            operation.next_attempt_at = context.next_attempt_at(operation.attempt_count)
            operation.error_code = "unknown_outcome"
            operation.error_message = "Provider release outcome is unknown."
        else:
            operation.status = ProviderOperationStatus.FAILED
            operation.next_attempt_at = None
            operation.error_code = attempt.outcome.value
            operation.error_message = "Provider did not release the hold."
        return attempt.outcome

    async def _ensure_order(
        self,
        reservation: ReservationModel,
    ) -> None:
        existing_order_statement = (
            select(OrderModel).where(OrderModel.reservation_id == reservation.id).with_for_update()
        )
        existing_order = (await self._session.scalars(existing_order_statement)).one_or_none()
        if existing_order is not None:
            return

        order = OrderModel(
            reservation_id=reservation.id,
            user_id=reservation.user_id,
        )
        self._session.add(order)
        await self._session.flush()

        items_statement = select(ReservationItemModel).where(
            ReservationItemModel.reservation_id == reservation.id
        )
        items = (await self._session.scalars(items_statement)).all()
        self._session.add_all(
            [
                OrderItemModel(
                    order_id=order.id,
                    product_id=item.product_id,
                    quantity=item.requested_quantity,
                )
                for item in items
            ]
        )

    async def _confirm_allocation(
        self,
        *,
        context: _ProviderOperationContext,
        allocation: InventoryAllocationModel,
        product_id: UUID,
        configured_provider: _ConfiguredProvider,
        inventory_repository: InventoryRepository,
    ) -> ProviderConfirmOutcome:
        provider = configured_provider.provider
        if allocation.status is AllocationStatus.CONFIRMED:
            return ProviderConfirmOutcome.CONFIRMED
        if provider.kind is ProviderKind.EXTERNAL:
            return await self._confirm_external_hold(
                context=context,
                allocation=allocation,
                provider=provider,
                credentials=configured_provider.credentials,
            )

        confirmed_inventory = await inventory_repository.confirm_hold(
            product_id=product_id,
            provider_id=allocation.provider_id,
            quantity=allocation.quantity,
        )
        if confirmed_inventory is None:
            raise _InventoryConfirmationError
        return ProviderConfirmOutcome.CONFIRMED

    async def _confirm_external_hold(
        self,
        *,
        context: _ProviderOperationContext,
        allocation: InventoryAllocationModel,
        provider: InventoryProviderModel,
        credentials: ProviderCredentialModel | None,
    ) -> ProviderConfirmOutcome:
        if (
            self._provider_registry is None
            or not provider.supports_confirm
            or provider.base_url is None
            or provider.driver != "http"
            or allocation.provider_hold_reference is None
        ):
            raise _InventoryConfirmationError

        idempotency_key = f"reservation:{context.reservation_id}:allocation:{allocation.id}:confirm"
        operation_statement = (
            select(ProviderOperationModel)
            .where(ProviderOperationModel.idempotency_key == idempotency_key)
            .with_for_update()
        )
        operation = (await self._session.scalars(operation_statement)).one_or_none()
        if operation is not None:
            if operation.status is ProviderOperationStatus.SUCCEEDED:
                return ProviderConfirmOutcome.CONFIRMED
            if operation.status is ProviderOperationStatus.UNKNOWN and (
                not context.retry_unknown or operation.attempt_count >= context.max_attempts
            ):
                return ProviderConfirmOutcome.UNKNOWN
            operation.status = ProviderOperationStatus.IN_PROGRESS
            operation.attempt_count += 1
            operation.next_attempt_at = None
            operation.error_code = None
            operation.error_message = None
        else:
            operation = ProviderOperationModel(
                allocation_id=allocation.id,
                operation=ProviderOperationType.CONFIRM,
                status=ProviderOperationStatus.IN_PROGRESS,
                idempotency_key=idempotency_key,
                attempt_count=1,
                external_reference=allocation.provider_hold_reference,
            )
            self._session.add(operation)
        await self._session.flush()

        external_provider = self._provider_registry.get_external(
            provider_id=provider.id,
            base_url=provider.base_url,
            timeout=provider.request_timeout_ms / 1000,
            credentials=_to_provider_credentials(credentials),
        )
        attempt = await external_provider.confirm(
            ConfirmCommand(
                hold_reference=allocation.provider_hold_reference,
                idempotency_key=idempotency_key,
            )
        )
        if attempt.outcome is ProviderConfirmOutcome.CONFIRMED:
            operation.status = ProviderOperationStatus.SUCCEEDED
            operation.next_attempt_at = None
        elif attempt.outcome is ProviderConfirmOutcome.UNKNOWN:
            operation.status = ProviderOperationStatus.UNKNOWN
            operation.next_attempt_at = context.next_attempt_at(operation.attempt_count)
            operation.error_code = "unknown_outcome"
            operation.error_message = "Provider confirmation outcome is unknown."
        else:
            operation.status = ProviderOperationStatus.FAILED
            operation.next_attempt_at = None
            operation.error_code = attempt.outcome.value
            operation.error_message = "Provider did not confirm the hold."
        return attempt.outcome

    @staticmethod
    def _to_model(reservation: Reservation) -> ReservationModel:
        reservation_model = ReservationModel(
            id=reservation.id,
            user_id=reservation.user_id,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            status=ReservationStatusModel(reservation.status.value),
            expires_at=reservation.expires_at,
            created_at=reservation.created_at,
            release_target_status=(
                ReservationStatusModel(reservation.release_target_status.value)
                if reservation.release_target_status is not None
                else None
            ),
        )
        reservation_model.items = [
            ReservationItemModel(
                reservation_id=reservation.id,
                product_id=item.product_id,
                provider_id=item.provider_id,
                requested_quantity=item.quantity,
            )
            for item in reservation.items
        ]
        return reservation_model

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
                    provider_id=item_model.provider_id,
                    quantity=item_model.requested_quantity,
                )
                for item_model in item_models
            ),
            idempotency_key=reservation_model.idempotency_key,
            request_fingerprint=reservation_model.request_fingerprint,
            created_at=reservation_model.created_at,
            expires_at=reservation_model.expires_at,
            status=ReservationStatus(reservation_model.status.value),
            release_target_status=(
                ReservationStatus(reservation_model.release_target_status.value)
                if reservation_model.release_target_status is not None
                else None
            ),
        )


@asynccontextmanager
async def reservation_transaction(
    database: Database,
    provider_registry: ProviderRegistry | None = None,
) -> AsyncIterator[ReservationRepositoryPort]:
    try:
        async with database.session() as session, session.begin():
            yield SqlAlchemyReservationRepository(
                session,
                provider_registry,
            )
    except IntegrityError as error:
        if _violates_constraint(error, IDEMPOTENCY_CONSTRAINT):
            raise ConcurrentReservationCreationError from error
        raise


def _violates_constraint(error: IntegrityError, constraint_name: str) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if getattr(cause, "constraint_name", None) == constraint_name:
            return True
        cause = cause.__cause__
    return False
