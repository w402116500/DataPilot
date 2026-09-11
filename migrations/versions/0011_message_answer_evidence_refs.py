"""为完整 Markdown 答案增加服务端派生的答案级证据引用。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_message_answer_evidence_refs"
down_revision = "0010_message_answer_sections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("messages")}
    if "answer_evidence_refs_json" in columns:
        return
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("answer_evidence_refs_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("answer evidence refs are intentionally irreversible")
