"""使没有准备阶段能力快照的旧模型配置重新进入待测试状态。"""

from __future__ import annotations

from alembic import op

revision = "0017_invalidate_legacy_preparation_capabilities"
down_revision = "0016_preparation_output_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE model_profiles
        SET status = 'created',
            tool_calling_supported = NULL,
            final_output_mode = NULL,
            capability_fingerprint = NULL,
            capability_checked_at = NULL
        WHERE preparation_output_mode IS NULL
        """
    )


def downgrade() -> None:
    raise RuntimeError("legacy capability invalidation is intentionally irreversible")
