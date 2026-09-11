"""移除跨会话长期记忆及其历史数据。"""

from __future__ import annotations

from alembic import op

revision = "0012_remove_long_term_memories"
down_revision = "0011_message_answer_evidence_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("long_term_memories")


def downgrade() -> None:
    raise RuntimeError("long-term memories removal is intentionally irreversible")
