"""允许最终答案模型直接返回 Markdown 原文。"""

from __future__ import annotations

from alembic import op

revision = "0013_direct_markdown_final_answer"
down_revision = "0012_remove_long_term_memories"
branch_labels = None
depends_on = None

_FINAL_OUTPUT_MODE_CHECK = (
    "final_output_mode IS NULL OR "
    "final_output_mode IN ('markdown', 'json_schema', 'json_object', 'submit_answer')"
)


def upgrade() -> None:
    # SQLite requires table recreation to replace a CHECK constraint.
    for table_name in ("model_profiles", "runs"):
        with op.batch_alter_table(table_name, recreate="always") as batch:
            batch.drop_constraint(f"ck_{table_name}_final_output_mode", type_="check")
            batch.create_check_constraint(
                f"ck_{table_name}_final_output_mode",
                _FINAL_OUTPUT_MODE_CHECK,
            )


def downgrade() -> None:
    raise RuntimeError("direct Markdown final answers are intentionally irreversible")
