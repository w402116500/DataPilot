"""Persist the bounded final-answer evidence snapshot."""

import sqlalchemy as sa
from alembic import op

revision = "0030_message_answer_evidence_snapshot"
down_revision = "0029_connection_datasources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("answer_evidence_snapshot_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "answer_evidence_snapshot_json")
