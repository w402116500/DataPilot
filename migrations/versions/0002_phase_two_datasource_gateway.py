"""prepare metadata for phase two datasource and gateway behavior

Revision ID: 0002_phase_two_datasource_gateway
Revises: 0001_foundation_metadata
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_phase_two_datasource_gateway"
down_revision = "0001_foundation_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 阶段二允许内部验证查询不属于任何正式 Run 或 Session。
    with op.batch_alter_table("sql_audit_logs") as batch:
        batch.alter_column("run_id", existing_type=sa.String(length=120), nullable=True)
        batch.alter_column("step_id", existing_type=sa.String(length=120), nullable=True)

    # 完整结果文件失败时，Artifact 仍要保存 preview_json，但没有 storage_ref。
    with op.batch_alter_table("artifacts") as batch:
        batch.alter_column("run_id", existing_type=sa.String(length=120), nullable=True)
        batch.alter_column("session_id", existing_type=sa.String(length=120), nullable=True)
        batch.alter_column("storage_ref", existing_type=sa.String(length=500), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.alter_column("storage_ref", existing_type=sa.String(length=500), nullable=False)
        batch.alter_column("session_id", existing_type=sa.String(length=120), nullable=False)
        batch.alter_column("run_id", existing_type=sa.String(length=120), nullable=False)

    with op.batch_alter_table("sql_audit_logs") as batch:
        batch.alter_column("step_id", existing_type=sa.String(length=120), nullable=False)
        batch.alter_column("run_id", existing_type=sa.String(length=120), nullable=False)
