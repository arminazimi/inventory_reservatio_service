"""Select a product offer explicitly during checkout.

Revision ID: 8e5d7c2a91b4
Revises: 6d92545a1a3f
Create Date: 2026-07-31 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e5d7c2a91b4"
down_revision: str | None = "6d92545a1a3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("inventory_levels", "product_offers")
    op.execute(
        "ALTER INDEX ix_inventory_levels_product_priority "
        "RENAME TO ix_product_offers_product_priority"
    )

    constraint_names = (
        "pk_inventory_levels",
        "uq_inventory_levels_product_id_provider_id",
        "ck_inventory_levels_non_negative_on_hand",
        "ck_inventory_levels_non_negative_reserved",
        "ck_inventory_levels_reserved_not_above_on_hand",
        "ck_inventory_levels_positive_version",
        "ck_inventory_levels_non_negative_allocation_priority",
        "fk_inventory_levels_product_id_products",
        "fk_inventory_levels_provider_id_inventory_providers",
    )
    for old_name in constraint_names:
        new_name = old_name.replace("inventory_levels", "product_offers")
        op.execute(f'ALTER TABLE product_offers RENAME CONSTRAINT "{old_name}" TO "{new_name}"')

    op.add_column(
        "reservation_items",
        sa.Column("provider_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        "UPDATE reservation_items AS item "
        "SET provider_id = allocation.provider_id "
        "FROM inventory_allocations AS allocation "
        "WHERE allocation.reservation_item_id = item.id"
    )
    # Old failed reservations may have no allocation. Preserve the former routing
    # semantics by recording the first provider that the old implementation would
    # have attempted for that product.
    op.execute(
        "UPDATE reservation_items AS item "
        "SET provider_id = ("
        "SELECT offer.provider_id FROM product_offers AS offer "
        "WHERE offer.product_id = item.product_id "
        "ORDER BY offer.allocation_priority, offer.provider_id LIMIT 1"
        ") WHERE item.provider_id IS NULL"
    )
    op.alter_column("reservation_items", "provider_id", nullable=False)
    op.create_foreign_key(
        "fk_reservation_items_provider_id_inventory_providers",
        "reservation_items",
        "inventory_providers",
        ["provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_reservation_items_provider_id_inventory_providers",
        "reservation_items",
        type_="foreignkey",
    )
    op.drop_column("reservation_items", "provider_id")

    constraint_names = (
        "pk_product_offers",
        "uq_product_offers_product_id_provider_id",
        "ck_product_offers_non_negative_on_hand",
        "ck_product_offers_non_negative_reserved",
        "ck_product_offers_reserved_not_above_on_hand",
        "ck_product_offers_positive_version",
        "ck_product_offers_non_negative_allocation_priority",
        "fk_product_offers_product_id_products",
        "fk_product_offers_provider_id_inventory_providers",
    )
    for old_name in constraint_names:
        new_name = old_name.replace("product_offers", "inventory_levels")
        op.execute(f'ALTER TABLE product_offers RENAME CONSTRAINT "{old_name}" TO "{new_name}"')

    op.execute(
        "ALTER INDEX ix_product_offers_product_priority "
        "RENAME TO ix_inventory_levels_product_priority"
    )
    op.rename_table("product_offers", "inventory_levels")
