"""允许 Run 用明确状态表示需要用户补充分析范围。"""

from __future__ import annotations

from alembic import op

revision = "0018_clarification_completion_kind"
down_revision = "0017_invalidate_legacy_preparation_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_runs_completion_kind", type_="check")
        batch.create_check_constraint(
            "ck_runs_completion_kind",
            "completion_kind IS NULL OR completion_kind IN "
            "('completed', 'partial', 'clarification')",
        )


def downgrade() -> None:
    raise RuntimeError("clarification completion kind is intentionally irreversible")
