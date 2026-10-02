"""切换到双协议 Run 路由并删除旧准备阶段能力字段。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_run_protocol_id"
down_revision = "0018_clarification_completion_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_profiles", recreate="always") as batch:
        batch.add_column(sa.Column("capability_contract_version", sa.String(length=40)))
        batch.drop_constraint("ck_model_profiles_preparation_output_mode", type_="check")
        batch.drop_column("preparation_output_mode")

    with op.batch_alter_table("runs", recreate="always") as batch:
        batch.add_column(sa.Column("protocol_id", sa.String(length=40)))
        batch.create_check_constraint(
            "ck_runs_protocol_id",
            "protocol_id IS NULL OR protocol_id IN ('general-task', 'data-analysis')",
        )
        batch.drop_constraint("ck_runs_preparation_output_mode", type_="check")
        batch.drop_column("preparation_output_mode")

    op.create_index(
        "uq_run_events_protocol_selected",
        "run_events",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("event_type = 'run.protocol.selected'"),
    )


def downgrade() -> None:
    raise RuntimeError("run protocol routing is intentionally irreversible")
