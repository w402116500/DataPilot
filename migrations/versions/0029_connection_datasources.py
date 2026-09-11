"""Add connection source identity and durable DataLink authorization."""

import sqlalchemy as sa
from alembic import op

revision = "0029_connection_datasources"
down_revision = "0028_session_delete_cascades_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_sources", sa.Column("source_kind", sa.String(20), nullable=False, server_default="file"))
    op.add_column("data_sources", sa.Column("connection_config_json", sa.JSON()))
    # SQLite supports a nullable REFERENCES column without rebuilding this parent table.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE data_sources ADD COLUMN credential_ref VARCHAR(120) REFERENCES secrets(id) ON DELETE RESTRICT")
    else:
        op.add_column("data_sources", sa.Column("credential_ref", sa.String(120), sa.ForeignKey("secrets.id", ondelete="RESTRICT")))
    op.add_column("data_sources", sa.Column("connection_fingerprint", sa.String(64)))
    op.add_column("data_sources", sa.Column("inspection_id", sa.String(120)))
    for table in ("data_sources", "runs", "sql_audit_logs", "datalink_validations"):
        op.add_column(table, sa.Column("connection_revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "connection_grants",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("datasource_id", sa.String(120), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rebuild_key", sa.String(120), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("connection_revision", sa.Integer(), nullable=False),
        sa.Column("build_id", sa.String(120)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_connection_grants_datasource_id", "connection_grants", ["datasource_id"])


def downgrade() -> None:
    op.drop_table("connection_grants")
    for table in ("data_sources", "runs", "sql_audit_logs", "datalink_validations"):
        op.drop_column(table, "connection_revision")
    for column in ("inspection_id", "connection_fingerprint", "credential_ref", "connection_config_json", "source_kind"):
        op.drop_column("data_sources", column)
