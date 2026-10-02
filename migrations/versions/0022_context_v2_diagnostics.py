"""complete context-v2 scope, provenance and budget diagnostics."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_context_v2_diagnostics"
down_revision = "0021_pending_context_snapshot"
branch_labels = None
depends_on = None

_HISTORICAL_SUMMARY_INDEX = "ix_historical_answer_summaries_session_datasource_finished"
_HISTORICAL_SUMMARY_INDEX_COLUMNS = [
    "session_id",
    "datasource_id",
    "source_run_finished_at",
    "source_run_id",
]


def upgrade() -> None:
    with op.batch_alter_table("model_profiles") as batch:
        batch.add_column(sa.Column("context_window_tokens", sa.Integer(), nullable=True))

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("historical_summary_projection_status", sa.String(24)))
        batch.add_column(
            sa.Column("historical_summary_projection_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("historical_summary_projection_last_attempt_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("context_window_tokens", sa.Integer()))
        batch.add_column(sa.Column("context_window_source", sa.String(24)))
        batch.add_column(sa.Column("input_budget_tokens", sa.Integer()))
        batch.add_column(sa.Column("output_budget_tokens", sa.Integer()))
        batch.add_column(sa.Column("system_budget_tokens", sa.Integer()))
        batch.add_column(sa.Column("tool_schema_cost", sa.Integer()))
        batch.add_column(sa.Column("safety_margin_tokens", sa.Integer()))

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite cannot alter an existing foreign key in place. Rebuild only
        # this metadata table; immutable Run/Event/Audit facts are untouched.
        op.create_table(
            "historical_answer_summaries_v2",
            sa.Column("id", sa.String(120), primary_key=True),
            sa.Column("session_id", sa.String(120), nullable=False),
            sa.Column("datasource_id", sa.String(120), nullable=False),
            sa.Column("source_run_id", sa.String(120), nullable=False),
            sa.Column("source_run_finished_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("topic", sa.String(300), nullable=False),
            sa.Column("content_text", sa.Text(), nullable=False),
            sa.Column("data_freshness", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["datasource_id"], ["data_sources.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("source_run_id", name="uq_historical_answer_summaries_source_run"),
        )
        op.execute(
            sa.text(
                """
                INSERT INTO historical_answer_summaries_v2
                (id, session_id, datasource_id, source_run_id, source_run_finished_at,
                 topic, content_text, data_freshness, created_at, updated_at)
                SELECT h.id, h.session_id, h.datasource_id, h.source_run_id,
                       COALESCE(r.finished_at, r.created_at, CURRENT_TIMESTAMP),
                       h.topic, h.content_text, h.data_freshness, h.created_at, h.updated_at
                FROM historical_answer_summaries h
                LEFT JOIN runs r ON r.id = h.source_run_id
                """
            )
        )
        op.drop_table("historical_answer_summaries")
        op.rename_table("historical_answer_summaries_v2", "historical_answer_summaries")
        op.create_index(
            _HISTORICAL_SUMMARY_INDEX,
            "historical_answer_summaries",
            _HISTORICAL_SUMMARY_INDEX_COLUMNS,
        )
    else:
        with op.batch_alter_table("historical_answer_summaries") as batch:
            batch.add_column(sa.Column("source_run_finished_at", sa.DateTime(timezone=True), nullable=True))
            batch.drop_constraint("historical_answer_summaries_source_run_id_fkey", type_="foreignkey")
            batch.create_foreign_key(
                "historical_answer_summaries_source_run_id_fkey",
                "runs",
                ["source_run_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        op.execute(
            sa.text(
                "UPDATE historical_answer_summaries h SET source_run_finished_at = "
                "COALESCE((SELECT finished_at FROM runs r WHERE r.id = h.source_run_id), h.created_at)"
            )
        )
        with op.batch_alter_table("historical_answer_summaries") as batch:
            batch.alter_column(
                "source_run_finished_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )
        op.drop_index(_HISTORICAL_SUMMARY_INDEX, table_name="historical_answer_summaries")
        op.create_index(
            _HISTORICAL_SUMMARY_INDEX,
            "historical_answer_summaries",
            _HISTORICAL_SUMMARY_INDEX_COLUMNS,
        )

    op.drop_table("conversation_summaries")


def downgrade() -> None:
    raise RuntimeError("context-v2 diagnostics are intentionally irreversible")
