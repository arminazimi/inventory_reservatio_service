"""Add the durable release target to reservations.

Revision ID: 2c984d2b8f1a
Revises: 57944397886c
Create Date: 2026-07-30 01:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2c984d2b8f1a"
down_revision: str | None = "57944397886c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("release_target_status", sa.String(length=20), nullable=True),
    )
    op.create_check_constraint(
        "ck_reservations_valid_release_target_status",
        "reservations",
        "release_target_status IS NULL OR release_target_status IN ('cancelled', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_reservations_valid_release_target_status",
        "reservations",
        type_="check",
    )
    op.drop_column("reservations", "release_target_status")
