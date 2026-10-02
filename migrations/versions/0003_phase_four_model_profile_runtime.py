"""add phase four model profile runtime settings

Revision ID: 0003_phase_four_model_profile_runtime
Revises: 0002_phase_two_datasource_gateway
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_phase_four_model_profile_runtime"
down_revision = "0002_phase_two_datasource_gateway"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_profiles") as batch:
        batch.add_column(
            sa.Column(
                "run_timeout_seconds",
                sa.Integer(),
                nullable=False,
                server_default="600",
            )
        )
        batch.create_check_constraint(
            "ck_model_profiles_run_timeout_seconds",
            "run_timeout_seconds >= 1 AND run_timeout_seconds <= 600",
        )


def downgrade() -> None:
    with op.batch_alter_table("model_profiles") as batch:
        batch.drop_constraint("ck_model_profiles_run_timeout_seconds", type_="check")
        batch.drop_column("run_timeout_seconds")
