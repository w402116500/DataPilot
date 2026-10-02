"""Add model profile reasoning mode."""

import sqlalchemy as sa
from alembic import op

revision = "0031_model_profile_reasoning_mode"
down_revision = "0030_message_answer_evidence_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_profiles") as batch:
        batch.add_column(
            sa.Column(
                "reasoning_mode",
                sa.String(length=20),
                nullable=False,
                server_default="default",
            )
        )
        batch.create_check_constraint(
            "ck_model_profiles_reasoning_mode",
            "reasoning_mode IN ('default', 'disabled', 'high', 'max')",
        )


def downgrade() -> None:
    with op.batch_alter_table("model_profiles") as batch:
        batch.drop_constraint("ck_model_profiles_reasoning_mode", type_="check")
        batch.drop_column("reasoning_mode")
