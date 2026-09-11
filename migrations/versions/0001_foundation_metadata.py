"""create foundation metadata tables

Revision ID: 0001_foundation_metadata
Revises:
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_foundation_metadata"
down_revision = None
branch_labels = None
depends_on = None


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("source_ref", sa.String(length=500), unique=True),
        sa.Column("file_size", sa.Integer()),
        sa.Column("content_hash", sa.String(length=128)),
        sa.Column("schema_cache_json", sa.JSON()),
        sa.Column("schema_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="uploaded"),
        sa.Column("datalink_build_id", sa.String(length=120)),
        sa.Column("datalink_graph_version", sa.String(length=120)),
        sa.Column("last_error_code", sa.String(length=80)),
        sa.Column("last_error_message", sa.Text()),
        sa.Column("last_test_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint("schema_revision >= 0", name="ck_data_sources_schema_revision"),
        sa.CheckConstraint(
            "status != 'ready' OR "
            "(schema_cache_json IS NOT NULL AND datalink_graph_version IS NOT NULL)",
            name="ck_data_sources_ready_has_schema_and_graph",
        ),
    )

    op.create_table(
        "secrets",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        *timestamps(),
    )

    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "secret_ref", sa.String(length=120), sa.ForeignKey("secrets.id", ondelete="SET NULL")
        ),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="created"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        *timestamps(),
    )
    op.create_index(
        "uq_model_profiles_single_active",
        "model_profiles",
        ["is_active"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "selected_datasource_id",
            sa.String(length=120),
            sa.ForeignKey("data_sources.id", ondelete="SET NULL"),
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=120), sa.ForeignKey("runs.id", ondelete="SET NULL")),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("artifact_refs_json", sa.JSON()),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "position", name="uq_messages_position"),
    )

    op.create_table(
        "runs",
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
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "user_message_id",
            sa.String(length=120),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "assistant_message_id",
            sa.String(length=120),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="queued"),
        sa.Column(
            "model_profile_id",
            sa.String(length=120),
            sa.ForeignKey("model_profiles.id", ondelete="SET NULL"),
        ),
        sa.Column("model_provider", sa.String(length=80)),
        sa.Column("model_name", sa.String(length=120)),
        sa.Column("schema_revision", sa.Integer()),
        sa.Column("datalink_graph_version", sa.String(length=120)),
        sa.Column("config_snapshot_json", sa.JSON()),
        sa.Column("analysis_plan_json", sa.JSON()),
        sa.Column(
            "current_step_id",
            sa.String(length=120),
            sa.ForeignKey("run_steps.id", ondelete="SET NULL"),
        ),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.String(length=120)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )

    op.create_table(
        "run_steps",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("expected_columns_json", sa.JSON()),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column(
            "result_artifact_id",
            sa.String(length=120),
            sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
        ),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint("run_id", "position", name="uq_run_steps_position"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(length=120),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id", sa.String(length=120), sa.ForeignKey("run_steps.id", ondelete="SET NULL")
        ),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("preview_json", sa.JSON()),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column(
            "step_id", sa.String(length=120), sa.ForeignKey("run_steps.id", ondelete="SET NULL")
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_seq"),
    )
    op.create_index("ix_run_events_run_seq", "run_events", ["run_id", "seq"])
    op.create_index("ix_run_events_run_type", "run_events", ["run_id", "event_type"])

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id", sa.String(length=120), sa.ForeignKey("run_steps.id", ondelete="SET NULL")
        ),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("input_json", sa.JSON()),
        sa.Column("output_summary_json", sa.JSON()),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "sql_audit_logs",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=120),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id", sa.String(length=120), sa.ForeignKey("run_steps.id", ondelete="CASCADE")
        ),
        sa.Column(
            "datasource_id",
            sa.String(length=120),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
        ),
        sa.Column("schema_revision", sa.Integer()),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "repaired_from_id",
            sa.String(length=120),
            sa.ForeignKey("sql_audit_logs.id", ondelete="SET NULL"),
        ),
        sa.Column("original_sql", sa.Text(), nullable=False),
        sa.Column("normalized_sql", sa.Text()),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="proposed"),
        sa.Column("statement_type", sa.String(length=40)),
        sa.Column("referenced_tables_json", sa.JSON()),
        sa.Column("blocked_reason_code", sa.String(length=80)),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column(
            "artifact_id", sa.String(length=120), sa.ForeignKey("artifacts.id", ondelete="SET NULL")
        ),
        sa.Column("row_count", sa.Integer()),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        *timestamps(),
        sa.UniqueConstraint("run_id", "step_id", "attempt_no", name="uq_sql_audit_attempt"),
    )

    op.create_table(
        "artifact_cleanup_tasks",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "artifact_id", sa.String(length=120), sa.ForeignKey("artifacts.id", ondelete="SET NULL")
        ),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=80)),
        *timestamps(),
    )


def downgrade() -> None:
    op.drop_table("artifact_cleanup_tasks")
    op.drop_table("sql_audit_logs")
    op.drop_table("tool_calls")
    op.drop_index("ix_run_events_run_type", table_name="run_events")
    op.drop_index("ix_run_events_run_seq", table_name="run_events")
    op.drop_table("run_events")
    op.drop_table("artifacts")
    op.drop_table("run_steps")
    op.drop_table("runs")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_index("uq_model_profiles_single_active", table_name="model_profiles")
    op.drop_table("model_profiles")
    op.drop_table("secrets")
    op.drop_table("data_sources")
