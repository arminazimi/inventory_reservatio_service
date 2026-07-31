"""Add explicit substitutable provider routing groups.

Revision ID: 21c8e4f6a2b9
Revises: 8e5d7c2a91b4
Create Date: 2026-07-31 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "21c8e4f6a2b9"
down_revision: str | None = "8e5d7c2a91b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product_offers",
        sa.Column("routing_group", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_product_offers_routing_group_priority",
        "product_offers",
        ["product_id", "routing_group", "allocation_priority"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_offers_routing_group_priority",
        table_name="product_offers",
    )
    op.drop_column("product_offers", "routing_group")
