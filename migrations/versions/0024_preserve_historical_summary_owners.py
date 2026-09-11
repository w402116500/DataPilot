"""Prevent owner deletion from cascading into immutable historical summaries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_preserve_historical_summary_owners"
down_revision = "0023_require_tool_call_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.create_table(
            "historical_answer_summaries_v2",
            sa.Column("id", sa.String(120), primary_key=True),
            sa.Column("session_id", sa.String(120), nullable=False),
            sa.Column("datasource_id", sa.String(120), nullable=False),
            sa.Column("source_run_id", sa.String(120), nullable=False),
            sa.Column("source_run_finished_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("topic", sa.String(300), nullable=False),
            sa.Column("content_text", sa.Text(), nullable=False),
            sa.Column(
                "provenance",
                sa.String(40),
                nullable=False,
                server_default="historical_answer_summary",
            ),
            sa.Column("data_freshness", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["datasource_id"], ["data_sources.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("source_run_id", name="uq_historical_answer_summaries_source_run"),
        )
        op.execute(
            sa.text(
                """
                INSERT INTO historical_answer_summaries_v2
                (id, session_id, datasource_id, source_run_id, source_run_finished_at,
                 topic, content_text, provenance, data_freshness, created_at, updated_at)
                SELECT id, session_id, datasource_id, source_run_id, source_run_finished_at,
                       topic, content_text, 'historical_answer_summary', data_freshness,
                       created_at, updated_at
                FROM historical_answer_summaries
                """
            )
        )
        op.drop_table("historical_answer_summaries")
        op.rename_table("historical_answer_summaries_v2", "historical_answer_summaries")
        op.create_index(
            "ix_historical_answer_summaries_session_datasource_finished",
            "historical_answer_summaries",
            ["session_id", "datasource_id", "source_run_finished_at", "source_run_id"],
        )
        return

    with op.batch_alter_table("historical_answer_summaries") as batch:
        batch.add_column(
            sa.Column(
                "provenance",
                sa.String(40),
                nullable=False,
                server_default="historical_answer_summary",
            )
        )
        batch.drop_constraint("historical_answer_summaries_session_id_fkey", type_="foreignkey")
        batch.drop_constraint("historical_answer_summaries_datasource_id_fkey", type_="foreignkey")
        batch.create_foreign_key(
            "historical_answer_summaries_session_id_fkey",
            "sessions",
            ["session_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "historical_answer_summaries_datasource_id_fkey",
            "data_sources",
            ["datasource_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    raise RuntimeError("historical summary owner retention is intentionally irreversible")
