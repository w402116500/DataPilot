from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunEventType(StrEnum):
    """统一工具运行事件；同一份事件账本同时服务 SSE 和历史回放。"""

    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_PREPARATION_STARTED = "run.preparation.started"
    RUN_PREPARATION_COMPLETED = "run.preparation.completed"
    RUN_PROTOCOL_SELECTED = "run.protocol.selected"
    ANALYSIS_CLARIFICATION_REQUESTED = "analysis.clarification.requested"
    ANALYSIS_REQUIREMENT_BLOCKED = "analysis.requirement.blocked"
    ANALYSIS_DISCOVERY_OBSERVED = "analysis.discovery.observed"
    AGENT_TURN_STARTED = "agent.turn.started"
    AGENT_TURN_COMPLETED = "agent.turn.completed"
    FINAL_ANSWER_REQUEST_STARTED = "final_answer.request.started"
    FINAL_ANSWER_RESPONSE_RECEIVED = "final_answer.response.received"
    FINAL_ANSWER_VALIDATION_FAILED = "final_answer.validation.failed"
    FINAL_ANSWER_REQUEST_TIMED_OUT = "final_answer.request.timed_out"
    TOOL_CALLED = "tool.called"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    ARTIFACT_CREATED = "artifact.created"
    ANSWER_DELTA = "answer.delta"
    ANSWER_READY = "answer.ready"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RUN_CANCELED = "run.canceled"


class RunEventCreate(BaseModel):
    """运行时创建事件的最小输入；payload 只能是安全摘要。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    type: RunEventType
    payload: dict[str, Any] = Field(default_factory=dict, max_length=40)
    tool_input: dict[str, object] | None = Field(default=None, exclude=True, repr=False)


class RunEventRead(BaseModel):
    """SSE、历史接口共用的事件 DTO。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int = Field(ge=1)
    type: RunEventType
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict, max_length=40)
