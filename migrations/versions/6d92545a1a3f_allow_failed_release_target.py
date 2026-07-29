"""Allow failed reservations to complete compensating releases.

Revision ID: 6d92545a1a3f
Revises: 2c984d2b8f1a
Create Date: 2026-07-30 16:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6d92545a1a3f"
down_revision: str | None = "2c984d2b8f1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "ck_reservations_valid_release_target_status"


def upgrade() -> None:
    op.drop_constraint(
        CONSTRAINT_NAME,
        "reservations",
        type_="check",
    )
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "reservations",
        (
            "release_target_status IS NULL OR "
            "release_target_status IN ('cancelled', 'expired', 'failed')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        CONSTRAINT_NAME,
        "reservations",
        type_="check",
    )
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "reservations",
        "release_target_status IS NULL OR release_target_status IN ('cancelled', 'expired')",
    )
