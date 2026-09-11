"""Cascade historical summaries when a Session or Run is deleted."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_session_delete_cascades_summaries"
down_revision = "0027_datalink_validation_cancel"
branch_labels = None
depends_on = None

_SUMMARY_COLUMNS = (
    "id",
    "session_id",
    "datasource_id",
    "source_run_id",
    "source_run_finished_at",
    "topic",
    "content_text",
    "provenance",
    "data_freshness",
    "created_at",
    "updated_at",
)


def _copy_summaries(source: str, target: str) -> None:
    columns = ", ".join(_SUMMARY_COLUMNS)
    op.execute(sa.text(f"INSERT INTO {target} ({columns}) SELECT {columns} FROM {source}"))


def _recreate_summaries(*, session_ondelete: str, run_ondelete: str) -> None:
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
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete=session_ondelete),
        sa.ForeignKeyConstraint(["datasource_id"], ["data_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"], ondelete=run_ondelete),
        sa.UniqueConstraint("source_run_id", name="uq_historical_answer_summaries_source_run"),
    )
    _copy_summaries("historical_answer_summaries", "historical_answer_summaries_v2")
    op.drop_table("historical_answer_summaries")
    op.rename_table("historical_answer_summaries_v2", "historical_answer_summaries")
    op.create_index(
        "ix_historical_answer_summaries_session_datasource_finished",
        "historical_answer_summaries",
        ["session_id", "datasource_id", "source_run_finished_at", "source_run_id"],
    )


def _replace_owner_fks(*, session_ondelete: str, run_ondelete: str) -> None:
    with op.batch_alter_table("historical_answer_summaries") as batch:
        batch.drop_constraint("historical_answer_summaries_session_id_fkey", type_="foreignkey")
        batch.drop_constraint("historical_answer_summaries_source_run_id_fkey", type_="foreignkey")
        batch.create_foreign_key(
            "historical_answer_summaries_session_id_fkey",
            "sessions",
            ["session_id"],
            ["id"],
            ondelete=session_ondelete,
        )
        batch.create_foreign_key(
            "historical_answer_summaries_source_run_id_fkey",
            "runs",
            ["source_run_id"],
            ["id"],
            ondelete=run_ondelete,
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _recreate_summaries(session_ondelete="CASCADE", run_ondelete="CASCADE")
        return
    _replace_owner_fks(session_ondelete="CASCADE", run_ondelete="CASCADE")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _recreate_summaries(session_ondelete="RESTRICT", run_ondelete="RESTRICT")
        return
    _replace_owner_fks(session_ondelete="RESTRICT", run_ondelete="RESTRICT")
