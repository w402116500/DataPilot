"""保存准备阶段结构化输出能力快照。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_preparation_output_mode"
down_revision = "0015_message_datasource_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("model_profiles", "runs"):
        with op.batch_alter_table(table_name, recreate="always") as batch:
            batch.add_column(sa.Column("preparation_output_mode", sa.String(length=20)))
            batch.create_check_constraint(
                f"ck_{table_name}_preparation_output_mode",
                "preparation_output_mode IS NULL OR preparation_output_mode IN ('json_schema', 'json_object')",
            )


def downgrade() -> None:
    for table_name in ("model_profiles", "runs"):
        with op.batch_alter_table(table_name, recreate="always") as batch:
            batch.drop_constraint(f"ck_{table_name}_preparation_output_mode", type_="check")
            batch.drop_column("preparation_output_mode")
