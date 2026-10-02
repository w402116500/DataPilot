"""require the current run-scoped tool-call identity."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_require_tool_call_identity"
down_revision = "0022_context_v2_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    missing = bind.execute(
        sa.text("SELECT COUNT(*) FROM tool_calls WHERE tool_call_id IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            "tool_calls contains rows without tool_call_id; remove obsolete tool-call metadata "
            "before applying the current run protocol"
        )

    with op.batch_alter_table("tool_calls") as batch:
        batch.alter_column(
            "tool_call_id",
            existing_type=sa.String(length=120),
            nullable=False,
        )


def downgrade() -> None:
    raise RuntimeError("tool-call identity requirement is intentionally irreversible")
