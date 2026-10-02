"""保存模型原始工具编号并允许跨 Run 重用。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_tool_call_run_scoped_identity"
down_revision = "0013_direct_markdown_final_answer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("tool_call_id", sa.String(length=120), nullable=True))
    op.create_index(
        "ix_tool_calls_run_tool_call_id",
        "tool_calls",
        ["run_id", "tool_call_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_calls_run_tool_call_id", table_name="tool_calls")
    op.drop_column("tool_calls", "tool_call_id")
