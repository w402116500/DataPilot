"""add phase five run runtime persistence foundation

Revision ID: 0005_phase_five_run_runtime_foundation
Revises: 0004_phase_four_run_secret_snapshots
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_phase_five_run_runtime_foundation"
down_revision = "0004_phase_four_run_secret_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐 Run 幂等与记忆表，旧 Run 的新增字段保持为空以便历史可读。"""

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=120)))
        batch.add_column(sa.Column("run_timeout_seconds", sa.Integer()))
        batch.create_unique_constraint(
            "uq_runs_session_idempotency_key",
            ["session_id", "idempotency_key"],
        )
        batch.create_check_constraint(
            "ck_runs_run_timeout_seconds",
            "run_timeout_seconds IS NULL OR "
            "(run_timeout_seconds >= 1 AND run_timeout_seconds <= 600)",
        )
    op.create_index(
        "uq_runs_single_active_session",
        "runs",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "conversation_summaries",
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("through_message_position", sa.Integer(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "long_term_memories",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "datasource_id",
            sa.String(length=120),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
        ),
        sa.Column("memory_type", sa.String(length=80), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "source_run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
        ),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'user' AND session_id IS NULL AND datasource_id IS NULL) OR "
            "(scope = 'session' AND session_id IS NOT NULL AND datasource_id IS NULL) OR "
            "(scope = 'datasource' AND session_id IS NULL AND datasource_id IS NOT NULL)",
            name="ck_long_term_memories_scope_owner",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_long_term_memories_confidence",
        ),
    )
    op.create_index(
        "ix_long_term_memories_scope_accessed",
        "long_term_memories",
        ["scope", "last_accessed_at"],
    )
    op.create_index(
        "ix_long_term_memories_session_accessed",
        "long_term_memories",
        ["session_id", "last_accessed_at"],
    )
    op.create_index(
        "ix_long_term_memories_datasource_accessed",
        "long_term_memories",
        ["datasource_id", "last_accessed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_long_term_memories_datasource_accessed", table_name="long_term_memories")
    op.drop_index("ix_long_term_memories_session_accessed", table_name="long_term_memories")
    op.drop_index("ix_long_term_memories_scope_accessed", table_name="long_term_memories")
    op.drop_table("long_term_memories")
    op.drop_table("conversation_summaries")
    op.drop_index("uq_runs_single_active_session", table_name="runs")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_runs_run_timeout_seconds", type_="check")
        batch.drop_constraint("uq_runs_session_idempotency_key", type_="unique")
        batch.drop_column("run_timeout_seconds")
        batch.drop_column("idempotency_key")
