from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.datalink import DataLinkExploreFocus, DataLinkSemanticContext

ValidationStatus = Literal["running", "completed", "partial", "failed", "canceled", "interrupted"]
ValidationDirection = Literal["source_to_target", "target_to_source"]
DataLinkConsumptionStage = Literal["prepare", "tool"]
DataLinkConsumptionMode = Literal["live", "cached"]
DataLinkConsumptionPayloadStatus = Literal["complete", "too_large", "unavailable"]
DataLinkConsumptionReturnedStatus = Literal[
    "ok", "no_match", "truncated", "unavailable", "too_large"
]
DataLinkConsumptionReceiptStatus = Literal["received", "ignored_empty", "not_received"]
DATALINK_CONSUMPTION_PAYLOAD_LIMIT_BYTES = 256 * 1024
DATALINK_CONSUMPTION_PAYLOAD_VERSION = 1


class DataLinkValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_id: str = Field(min_length=1, max_length=300)
    graph_version: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120)


class DataLinkValidationRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    datasource_id: str
    relation_id: str
    graph_version: str
    schema_revision: int
    status: ValidationStatus
    source_non_null_count: int | None = Field(default=None, ge=0)
    target_non_null_count: int | None = Field(default=None, ge=0)
    source_distinct_count: int | None = Field(default=None, ge=0)
    target_distinct_count: int | None = Field(default=None, ge=0)
    target_duplicate_count: int | None = Field(default=None, ge=0)
    source_unmatched_count: int | None = Field(default=None, ge=0)
    multiple_match_risk: bool | None = None
    direction: ValidationDirection
    endpoint_fingerprint: str
    audit_log_ids: list[str] = Field(default_factory=list, max_length=20)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    error_code: str | None = None
    expired: bool = False
    created_at: datetime
    finished_at: datetime | None = None


class DataLinkConsumptionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_count: int = Field(default=0, ge=0)
    relationship_count: int = Field(default=0, ge=0)
    join_path_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    payload_bytes: int = Field(default=0, ge=0)


class DataLinkConsumptionCreate(BaseModel):
    """RunEventPipeline 写入的安全消费记录，不含原始 MCP 或 ToolMessage。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    stage: DataLinkConsumptionStage
    query: str = Field(min_length=1, max_length=2_000)
    focus: DataLinkExploreFocus | None = None
    max_nodes: int = Field(ge=1, le=50)
    schema_revision: int = Field(ge=0)
    graph_version: str | None = Field(default=None, max_length=120)
    mode: DataLinkConsumptionMode | None = None
    payload_status: DataLinkConsumptionPayloadStatus
    returned_status: DataLinkConsumptionReturnedStatus
    consumer_receipt_status: DataLinkConsumptionReceiptStatus
    is_truncated: bool = False
    tool_call_id: str | None = Field(default=None, max_length=120)
    payload_version: int = Field(default=DATALINK_CONSUMPTION_PAYLOAD_VERSION, ge=1)
    semantic_context: DataLinkSemanticContext | None = None
    summary: DataLinkConsumptionSummary = Field(default_factory=DataLinkConsumptionSummary)


class DataLinkConsumptionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    stage: DataLinkConsumptionStage
    seq: int = Field(ge=1)
    query: str
    focus: DataLinkExploreFocus | None = None
    max_nodes: int = Field(ge=1, le=50)
    schema_revision: int = Field(ge=0)
    graph_version: str | None = None
    mode: DataLinkConsumptionMode | None = None
    payload_status: DataLinkConsumptionPayloadStatus
    returned_status: DataLinkConsumptionReturnedStatus
    consumer_receipt_status: DataLinkConsumptionReceiptStatus
    is_truncated: bool = False
    tool_call_id: str | None = None
    payload_version: int = Field(ge=1)
    semantic_context: DataLinkSemanticContext | None = None
    summary: DataLinkConsumptionSummary
    created_at: datetime


class DataLinkConsumptionListRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    historical_status: Literal["recorded", "missing"]
    items: list[DataLinkConsumptionRead] = Field(default_factory=list, max_length=200)
