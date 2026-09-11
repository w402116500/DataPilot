from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from contracts.status import MessageRole


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    selected_datasource_id: str | None = Field(default=None, max_length=120)


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    selected_datasource_id: str | None = Field(default=None, max_length=120)


class SessionRead(BaseModel):
    id: str
    title: str
    selected_datasource_id: str | None
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


class MessageRead(BaseModel):
    """会话历史中的一条已保存消息，不附带运行时上下文或原始结果。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    run_id: str | None
    role: MessageRole
    content_text: str
    answer_evidence_refs: list[str] | None
    position: int = Field(ge=1)
    created_at: datetime
