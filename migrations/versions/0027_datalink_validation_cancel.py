"""Add cancel_requested to DataLink validation tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_datalink_validation_cancel"
down_revision = "0026_datalink_consumptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "datalink_validations",
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("datalink_validations", "cancel_requested")
