"""收缩核心运行链路，移除固定分析步骤并加入显式数据边界字段。

这是本地 Demo 的破坏性迁移。旧 RunStep 记录不再作为新历史回放来源。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_core_runtime_refactor"
down_revision = "0005_phase_five_run_runtime_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("data_sources") as batch:
        batch.drop_constraint("ck_data_sources_ready_has_schema_and_graph", type_="check")
        batch.add_column(sa.Column("mask_fields_json", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column(
                "mask_fields_confirmed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("input_snapshot_ref", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("mask_fields_json", sa.JSON(), nullable=True))
        batch.drop_column("current_step_id")
        batch.drop_column("analysis_plan_json")
        batch.drop_column("config_snapshot_json")

    for table in ("run_events", "tool_calls"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("step_id")

    with op.batch_alter_table("sql_audit_logs") as batch:
        batch.drop_constraint("uq_sql_audit_attempt", type_="unique")
        batch.drop_column("step_id")

    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("step_id")
        batch.alter_column("storage_ref", existing_type=sa.String(length=500), nullable=True)

    op.drop_table("run_steps")


def downgrade() -> None:
    raise RuntimeError("core runtime refactor migration is intentionally irreversible")
