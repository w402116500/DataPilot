"""将消息逐段答案改为唯一的正式存储字段。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_message_answer_sections"
down_revision = "0009_run_completion_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("messages")}
    if "answer_sections_json" in columns:
        return
    with op.batch_alter_table("messages") as batch:
        batch.alter_column(
            "artifact_refs_json",
            new_column_name="answer_sections_json",
            existing_type=sa.JSON(),
            existing_nullable=True,
        )


def downgrade() -> None:
    raise RuntimeError("canonical message answer sections are intentionally irreversible")
