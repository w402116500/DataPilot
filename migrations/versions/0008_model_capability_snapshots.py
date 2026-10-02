"""持久化模型能力缓存与 Run 的最终答案模式快照。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_model_capability_snapshots"
down_revision = "0007_reconcile_core_runtime_columns"
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

    profile_columns = _table_columns(bind, "model_profiles")
    with op.batch_alter_table("model_profiles") as batch:
        if "tool_calling_supported" not in profile_columns:
            batch.add_column(sa.Column("tool_calling_supported", sa.Boolean(), nullable=True))
        if "final_output_mode" not in profile_columns:
            batch.add_column(sa.Column("final_output_mode", sa.String(length=30), nullable=True))
        if "capability_fingerprint" not in profile_columns:
            batch.add_column(
                sa.Column("capability_fingerprint", sa.String(length=128), nullable=True)
            )
        if "capability_checked_at" not in profile_columns:
            batch.add_column(
                sa.Column("capability_checked_at", sa.DateTime(timezone=True), nullable=True)
            )
        if not _has_check_constraint(bind, "model_profiles", "ck_model_profiles_final_output_mode"):
            batch.create_check_constraint(
                "ck_model_profiles_final_output_mode",
                "final_output_mode IS NULL OR "
                "final_output_mode IN ('json_schema', 'json_object', 'submit_answer')",
            )

    run_columns = _table_columns(bind, "runs")
    with op.batch_alter_table("runs") as batch:
        if "final_output_mode" not in run_columns:
            batch.add_column(sa.Column("final_output_mode", sa.String(length=30), nullable=True))
        if "model_capability_fingerprint" not in run_columns:
            batch.add_column(
                sa.Column("model_capability_fingerprint", sa.String(length=128), nullable=True)
            )
        if not _has_check_constraint(bind, "runs", "ck_runs_final_output_mode"):
            batch.create_check_constraint(
                "ck_runs_final_output_mode",
                "final_output_mode IS NULL OR "
                "final_output_mode IN ('json_schema', 'json_object', 'submit_answer')",
            )


def downgrade() -> None:
    raise RuntimeError("model capability snapshots are intentionally irreversible")
