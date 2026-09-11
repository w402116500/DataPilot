"""补齐核心运行重构后被旧本地数据库遗漏的字段。

0006 在本地 Demo 数据库已经执行过后，工作树中的模型又增加了
``tool_call_id``。这个迁移只做结构对账，不删除或重写任何历史数据。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_reconcile_core_runtime_columns"
down_revision = "0006_core_runtime_refactor"
branch_labels = None
depends_on = None


def _table_columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _has_unique_constraint(
    bind: sa.Connection,
    table_name: str,
    column_names: set[str],
) -> bool:
    inspector = sa.inspect(bind)
    return any(
        set(constraint.get("column_names") or []) == column_names
        for constraint in inspector.get_unique_constraints(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    if "tool_call_id" not in _table_columns(bind, "sql_audit_logs"):
        with op.batch_alter_table("sql_audit_logs") as batch:
            batch.add_column(sa.Column("tool_call_id", sa.String(length=120), nullable=True))

    if "tool_call_id" not in _table_columns(bind, "artifacts"):
        with op.batch_alter_table("artifacts") as batch:
            batch.add_column(sa.Column("tool_call_id", sa.String(length=120), nullable=True))

    if not _has_unique_constraint(bind, "sql_audit_logs", {"run_id", "tool_call_id", "attempt_no"}):
        with op.batch_alter_table("sql_audit_logs") as batch:
            batch.create_unique_constraint(
                "uq_sql_audit_attempt",
                ["run_id", "tool_call_id", "attempt_no"],
            )


def downgrade() -> None:
    raise RuntimeError("core runtime column reconciliation is intentionally irreversible")
