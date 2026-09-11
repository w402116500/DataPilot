"""Persist DataLink relation validation tasks and reports."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_datalink_validations"
down_revision = "0024_preserve_historical_summary_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "datalink_validations",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column(
            "datasource_id",
            sa.String(120),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relation_id", sa.String(300), nullable=False),
        sa.Column("graph_version", sa.String(120), nullable=False),
        sa.Column("schema_revision", sa.Integer, nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="running"),
        sa.Column("direction", sa.String(40), nullable=False),
        sa.Column("endpoint_fingerprint", sa.String(128), nullable=False),
        sa.Column("metrics_json", sa.JSON),
        sa.Column("audit_log_ids_json", sa.JSON),
        sa.Column("artifact_ids_json", sa.JSON),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "datasource_id", "idempotency_key", name="uq_datalink_validation_idempotency"
        ),
        sa.Index("ix_datalink_validations_datasource_created", "datasource_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("datalink_validations")
