"""Persist Run-scoped DataLink consumption history."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_datalink_consumptions"
down_revision = "0025_datalink_validations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_datalink_consumptions",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("focus", sa.String(40)),
        sa.Column("max_nodes", sa.Integer, nullable=False),
        sa.Column("schema_revision", sa.Integer, nullable=False),
        sa.Column("graph_version", sa.String(120)),
        sa.Column("mode", sa.String(20)),
        sa.Column("payload_status", sa.String(40), nullable=False),
        sa.Column("returned_status", sa.String(40), nullable=False),
        sa.Column("consumer_receipt_status", sa.String(40), nullable=False),
        sa.Column("is_truncated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("tool_call_id", sa.String(120)),
        sa.Column("payload_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("payload_json", sa.JSON),
        sa.Column("summary_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_datalink_consumption_seq"),
        sa.Index("ix_run_datalink_consumptions_run_seq", "run_id", "seq"),
    )


def downgrade() -> None:
    op.drop_table("run_datalink_consumptions")
