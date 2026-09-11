"""freeze pending clarification identity on each Run."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_pending_context_snapshot"
down_revision = "0020_structured_conversation_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("pending_id_snapshot", sa.String(length=120)))
        batch.add_column(sa.Column("pending_revision_snapshot", sa.Integer()))


def downgrade() -> None:
    raise RuntimeError("pending context snapshots are intentionally irreversible")
