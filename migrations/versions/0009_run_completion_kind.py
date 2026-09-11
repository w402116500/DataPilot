"""记录成功 Run 的完整度，供历史回放明确区分完整与不完整回答。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_run_completion_kind"
down_revision = "0008_model_capability_snapshots"
branch_labels = None
depends_on = None


def _table_columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _has_check_constraint(bind: sa.Connection, table_name: str, name: str) -> bool:
    return any(
        constraint.get("name") == name
        for constraint in sa.inspect(bind).get_check_constraints(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    columns = _table_columns(bind, "runs")
    with op.batch_alter_table("runs") as batch:
        if "completion_kind" not in columns:
            batch.add_column(sa.Column("completion_kind", sa.String(length=20), nullable=True))
        if "incomplete_reason" not in columns:
            batch.add_column(sa.Column("incomplete_reason", sa.String(length=120), nullable=True))
        if not _has_check_constraint(bind, "runs", "ck_runs_completion_kind"):
            batch.create_check_constraint(
                "ck_runs_completion_kind",
                "completion_kind IS NULL OR completion_kind IN ('completed', 'partial')",
            )


def downgrade() -> None:
    raise RuntimeError("run completion kind is intentionally irreversible")
