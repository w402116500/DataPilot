"""add phase four run secret snapshots

Revision ID: 0004_phase_four_run_secret_snapshots
Revises: 0003_phase_four_model_profile_runtime
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_phase_four_run_secret_snapshots"
down_revision = "0003_phase_four_model_profile_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存 Run 自己的加密密钥引用，避免 Profile 后续轮换影响已启动 Run。"""

    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column(
                "model_secret_ref",
                sa.String(length=120),
            )
        )
        batch.create_foreign_key(
            "fk_runs_model_secret_ref_secrets",
            "secrets",
            ["model_secret_ref"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("fk_runs_model_secret_ref_secrets", type_="foreignkey")
        batch.drop_column("model_secret_ref")
