"""当前 Run 的只读 Trace DAG wire contract。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

TraceDagScalar = str | int | float | bool | None


class TraceDagNodeKind(StrEnum):
    RUN_START = "run-start"
    PREPARATION = "preparation"
    AGENT_TURN = "agent-turn"
    TOOL = "tool"
    ARTIFACT = "artifact"
    FINAL_ANSWER = "final-answer"
    RUN_TERMINAL = "run-terminal"


class TraceDagEdgeKind(StrEnum):
    STARTS = "starts"
    CONTINUES = "continues"
    INVOKES = "invokes"
    PRODUCES_ARTIFACT = "produces_artifact"
    COMPLETES = "completes"


class TraceDagRelationshipStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class TraceDagActionKind(StrEnum):
    RUN_STARTED = "run_started"
    PROTOCOL_SELECTED = "protocol_selected"
    PREPARATION_STARTED = "preparation_started"
    PREPARATION_COMPLETED = "preparation_completed"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_COMPLETED = "tool_completed"
    DISCOVERY_OBSERVED = "discovery_observed"
    ARTIFACT_REGISTERED = "artifact_registered"
    CLAIM_COMMITTED = "claim_committed"
    ANSWER_REQUESTED = "answer_requested"
    ANSWER_VALIDATED = "answer_validated"
    TERMINAL_RECORDED = "terminal_recorded"


class TraceDagActionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=300)
    kind: TraceDagActionKind
    event_seq: int = Field(ge=1)
    status: str | None = Field(default=None, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=500)
    tool_call_id: str | None = Field(default=None, max_length=120)
    artifact_id: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=160)


class TraceDagNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=500)
    kind: TraceDagNodeKind
    run_id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    start_seq: int | None = Field(default=None, ge=1)
    end_seq: int | None = Field(default=None, ge=1)
    status: str | None = Field(default=None, max_length=40)
    summary: str | None = Field(default=None, max_length=500)
    turn_no: int | None = Field(default=None, ge=1, le=10_000)
    tool_call_id: str | None = Field(default=None, max_length=120)
    artifact_id: str | None = Field(default=None, max_length=120)
    detail_event_seq: int | None = Field(default=None, ge=1)
    relationship_status: TraceDagRelationshipStatus = TraceDagRelationshipStatus.RESOLVED
    action_records: list[TraceDagActionRecord] = Field(default_factory=list, max_length=200)
    detail: dict[str, TraceDagScalar] = Field(default_factory=dict, max_length=40)


class TraceDagEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    kind: TraceDagEdgeKind
    label: str | None = Field(default=None, max_length=80)


class TraceDagSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    start_seq: int = Field(ge=1)
    end_seq: int = Field(ge=1)
    node_ids: list[str] = Field(default_factory=list, max_length=200)


class TraceDagRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    nodes: list[TraceDagNode] = Field(default_factory=list, max_length=2000)
    edges: list[TraceDagEdge] = Field(default_factory=list, max_length=4000)
    sections: list[TraceDagSection] = Field(default_factory=list, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=100)
