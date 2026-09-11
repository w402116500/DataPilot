"""为会话消息和摘要增加数据源范围。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_message_datasource_scope"
down_revision = "0014_tool_call_run_scoped_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "datasource_id",
                sa.String(length=120),
                sa.ForeignKey(
                    "data_sources.id",
                    name="fk_messages_datasource_id_data_sources",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    op.create_index(
        "ix_messages_session_datasource_position",
        "messages",
        ["session_id", "datasource_id", "position"],
    )
    with op.batch_alter_table("conversation_summaries") as batch_op:
        batch_op.add_column(
            sa.Column(
                "datasource_id",
                sa.String(length=120),
                sa.ForeignKey(
                    "data_sources.id",
                    name="fk_conversation_summaries_datasource_id_data_sources",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_summaries") as batch_op:
        batch_op.drop_column("datasource_id")
    op.drop_index("ix_messages_session_datasource_position", table_name="messages")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("datasource_id")
