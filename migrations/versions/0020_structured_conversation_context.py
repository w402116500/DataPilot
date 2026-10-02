"""add structured session context projections and run diagnostics."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_structured_conversation_context"
down_revision = "0019_run_protocol_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_preferences",
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("through_message_position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("preferences_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "conversation_context_states",
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "datasource_id",
            sa.String(length=120),
            sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_id", sa.String(length=120), unique=True),
        sa.Column("pending_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_context_states_active",
        "conversation_context_states",
        ["session_id", "datasource_id", "revision"],
    )
    op.create_table(
        "historical_answer_summaries",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "datasource_id",
            sa.String(length=120),
            sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("topic", sa.String(length=300), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("data_freshness", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_run_id", name="uq_historical_answer_summaries_source_run"),
    )
    op.create_index(
        "ix_historical_answer_summaries_session_datasource_finished",
        "historical_answer_summaries",
        ["session_id", "datasource_id", "created_at"],
    )
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("session_preferences_revision", sa.Integer()))
        batch.add_column(sa.Column("datasource_context_revision", sa.Integer()))
        batch.add_column(sa.Column("context_through_message_position", sa.Integer()))
        batch.add_column(sa.Column("context_projection_version", sa.String(length=40)))
        batch.add_column(sa.Column("context_snapshot_hash", sa.String(length=128)))
        batch.add_column(sa.Column("context_load_status", sa.String(length=20)))
        batch.add_column(sa.Column("historical_summary_ids_json", sa.JSON()))
        batch.add_column(sa.Column("historical_summary_count", sa.Integer()))
        batch.add_column(sa.Column("answer_data_freshness", sa.String(length=40)))
        batch.add_column(sa.Column("historical_context_injected", sa.Boolean()))


def downgrade() -> None:
    raise RuntimeError("structured conversation context is intentionally irreversible")
