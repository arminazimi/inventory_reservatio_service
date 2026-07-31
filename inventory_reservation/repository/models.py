from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid7

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class ProviderKind(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class ProviderAuthType(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"
    OAUTH2 = "oauth2"


class ReservationStatus(StrEnum):
    PENDING = "pending"
    RESERVING = "reserving"
    ACTIVE = "active"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    RELEASING = "releasing"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class AllocationStatus(StrEnum):
    PENDING = "pending"
    HELD = "held"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProviderOperationType(StrEnum):
    CHECK = "check"
    HOLD = "hold"
    CONFIRM = "confirm"
    RELEASE = "release"


class ProviderOperationStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ProductModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(sku) > 0", name="sku_not_empty"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
    )

    sku: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class InventoryProviderModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inventory_providers"
    __table_args__ = (
        CheckConstraint(
            "kind != 'external' OR base_url IS NOT NULL",
            name="external_provider_requires_base_url",
        ),
        CheckConstraint("request_timeout_ms > 0", name="positive_request_timeout"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    kind: Mapped[ProviderKind] = mapped_column(
        Enum(
            ProviderKind,
            name="provider_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    driver: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500))
    request_timeout_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2000,
        server_default=text("2000"),
    )
    supports_availability: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    supports_hold: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    supports_confirm: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    supports_release: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class ProviderCredentialModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("provider_id"),
        CheckConstraint(
            "auth_type = 'none' OR secret_ref IS NOT NULL",
            name="authenticated_provider_requires_secret_ref",
        ),
    )

    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    auth_type: Mapped[ProviderAuthType] = mapped_column(
        Enum(
            ProviderAuthType,
            name="provider_auth_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ProviderAuthType.NONE,
        server_default=text("'none'"),
    )
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    public_config: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class ProductOfferModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_offers"
    __table_args__ = (
        UniqueConstraint("product_id", "provider_id"),
        CheckConstraint("on_hand >= 0", name="non_negative_on_hand"),
        CheckConstraint("reserved >= 0", name="non_negative_reserved"),
        CheckConstraint("reserved <= on_hand", name="reserved_not_above_on_hand"),
        CheckConstraint("version >= 1", name="positive_version"),
        CheckConstraint("allocation_priority >= 0", name="non_negative_allocation_priority"),
        Index(
            "ix_product_offers_product_priority",
            "product_id",
            "allocation_priority",
        ),
        Index(
            "ix_product_offers_routing_group_priority",
            "product_id",
            "routing_group",
            "allocation_priority",
        ),
    )

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    on_hand: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    reserved: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    allocation_priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        server_default=text("100"),
    )
    routing_group: Mapped[UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReservationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_not_empty"),
        CheckConstraint("length(request_fingerprint) = 64", name="request_fingerprint_sha256"),
        CheckConstraint(
            (
                "release_target_status IS NULL OR "
                "release_target_status IN ('cancelled', 'expired', 'failed')"
            ),
            name="valid_release_target_status",
        ),
        CheckConstraint("version >= 1", name="positive_version"),
        Index("ix_reservations_expiry_worker", "status", "expires_at"),
        Index("ix_reservations_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(
            ReservationStatus,
            name="reservation_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ReservationStatus.PENDING,
        server_default=text("'pending'"),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    release_target_status: Mapped[ReservationStatus | None] = mapped_column(
        Enum(
            ReservationStatus,
            name="reservation_release_target_status",
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    items: Mapped[list[ReservationItemModel]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
    )


class ReservationItemModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reservation_items"
    __table_args__ = (
        UniqueConstraint("reservation_id", "product_id"),
        CheckConstraint("requested_quantity > 0", name="positive_requested_quantity"),
    )

    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation: Mapped[ReservationModel] = relationship(back_populates="items")


class InventoryAllocationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inventory_allocations"
    __table_args__ = (
        UniqueConstraint("reservation_item_id", "provider_id"),
        UniqueConstraint("hold_idempotency_key"),
        CheckConstraint("quantity > 0", name="positive_quantity"),
    )

    reservation_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("reservation_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AllocationStatus] = mapped_column(
        Enum(
            AllocationStatus,
            name="allocation_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=AllocationStatus.PENDING,
        server_default=text("'pending'"),
    )
    hold_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_hold_reference: Mapped[str | None] = mapped_column(String(255))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class ProviderOperationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider_operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint("attempt_count >= 0", name="non_negative_attempt_count"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="non_negative_latency"),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="valid_http_status",
        ),
        Index("ix_provider_operations_retry_queue", "status", "next_attempt_at"),
    )

    allocation_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_allocations.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation: Mapped[ProviderOperationType] = mapped_column(
        Enum(
            ProviderOperationType,
            name="provider_operation_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )
    status: Mapped[ProviderOperationStatus] = mapped_column(
        Enum(
            ProviderOperationStatus,
            name="provider_operation_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
        default=ProviderOperationStatus.PENDING,
        server_default=text("'pending'"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(255))
    external_reference: Mapped[str | None] = mapped_column(String(255))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class OrderModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("reservation_id"),)

    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class OrderItemModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "product_id"),
        CheckConstraint("quantity > 0", name="positive_quantity"),
    )

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
