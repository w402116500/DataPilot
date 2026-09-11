"""Run 创建、历史读取与阶段四遗留快照的共享契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from contracts.model_profiles import FinalOutputMode
from contracts.status import ArtifactType, RunStatus

CompletionKind = Literal["completed", "partial", "clarification"]
RunProtocolId = Literal["general-task", "data-analysis"]
type ToolInputValue = str | int | list[str] | None
type ToolInputParams = dict[str, ToolInputValue]


def project_tool_input_params(tool_name: str, value: object) -> ToolInputParams | None:
    """按已注册工具投影可展示参数，未知或类型错误的输入不进入历史 DTO。"""

    if not isinstance(value, dict):
        return None
    if tool_name == "run_sql_readonly":
        sql = value.get("sql")
        return {"sql": sql} if isinstance(sql, str) else None
    if tool_name == "run_python":
        script = value.get("script")
        output_paths = value.get("output_paths")
        purpose = value.get("purpose")
        if (
            not isinstance(script, str)
            or not isinstance(output_paths, list)
            or not all(isinstance(path, str) for path in output_paths)
            or not isinstance(purpose, str)
        ):
            return None
        return {
            "script": script,
            "output_paths": output_paths,
            "purpose": purpose,
        }
    if tool_name == "explore_datalink":
        query = value.get("query")
        if not isinstance(query, str):
            return None
        projected: ToolInputParams = {"query": query}
        if "focus" in value:
            focus = value["focus"]
            if focus is not None and not isinstance(focus, str):
                return None
            projected["focus"] = focus
        if "max_nodes" in value:
            max_nodes = value["max_nodes"]
            if type(max_nodes) is not int:
                return None
            projected["max_nodes"] = max_nodes
        return projected
    if tool_name == "commit_analysis_claims":
        claims = value.get("claims")
        if isinstance(claims, list) and claims and all(isinstance(item, dict) for item in claims):
            requirement_ids = [
                item["requirement_id"]
                for item in claims
                if isinstance(item.get("requirement_id"), str)
            ]
            evidence_binding_ids = [
                evidence_id
                for item in claims
                if isinstance(item.get("evidence_binding_ids"), list)
                for evidence_id in item["evidence_binding_ids"]
                if isinstance(evidence_id, str)
            ]
            if len(requirement_ids) == len(claims):
                return {
                    "claim_count": len(claims),
                    "requirement_ids": list(dict.fromkeys(requirement_ids))[:16],
                    "evidence_binding_ids": list(dict.fromkeys(evidence_binding_ids))[:64],
                }

        # RunEventPipeline stores this already-sanitized projection in input_json;
        # accept it as the canonical persisted form without reopening raw claims.
        claim_count = value.get("claim_count")
        requirement_ids = value.get("requirement_ids")
        evidence_binding_ids = value.get("evidence_binding_ids")
        if (
            type(claim_count) is int
            and claim_count >= 1
            and isinstance(requirement_ids, list)
            and all(isinstance(item, str) for item in requirement_ids)
            and isinstance(evidence_binding_ids, list)
            and all(isinstance(item, str) for item in evidence_binding_ids)
        ):
            return {
                "claim_count": claim_count,
                "requirement_ids": list(dict.fromkeys(requirement_ids))[:16],
                "evidence_binding_ids": list(dict.fromkeys(evidence_binding_ids))[:64],
            }
    return None


class RunCreate(BaseModel):
    """创建一次分析 Run 的唯一请求体，不允许本次请求临时换数据源。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=120)


class RunCreateAccepted(BaseModel):
    """Run 已持久化并等待后台启动时返回的稳定摘要。"""

    run_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    status: RunStatus


class RunRead(BaseModel):
    """历史 Run 的安全投影，不返回进程内上下文、密钥或 GraphState。"""

    id: str
    session_id: str
    datasource_id: str | None
    datasource_deleted: bool = False
    user_message_id: str | None
    question: str
    status: RunStatus
    protocol_id: RunProtocolId | None
    model_profile_id: str | None
    model_provider: str | None
    model_name: str | None
    schema_revision: int | None = Field(default=None, ge=0)
    connection_revision: int = Field(default=0, ge=0)
    datalink_graph_version: str | None
    has_input_snapshot: bool = False
    run_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    completion_kind: CompletionKind | None = None
    incomplete_reason: str | None = Field(default=None, max_length=120)
    error_code: str | None
    error_message: str | None
    cancel_requested_at: datetime | None
    cancel_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    answer_data_freshness: (
        Literal[
            "not_queried",
            "current_schema",
            "current_run_observation_only",
            "current_run_evidence",
        ]
        | None
    ) = None
    historical_context_injected: bool = False
    historical_summary_count: int = Field(default=0, ge=0, le=3)


class RunCancelRequest(BaseModel):
    """取消命令只接受稳定原因码，不能把任意诊断文字写入历史。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(
        default="user_requested",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9_.:-]{1,120}$",
    )


class RunCancelRead(BaseModel):
    """取消命令已登记后的当前 Run 状态。"""

    run_id: str
    status: RunStatus
    cancel_requested_at: datetime | None


class ToolCallRead(BaseModel):
    """工具调用的安全历史投影，只返回白名单参数和脱敏摘要。"""

    id: str
    run_id: str
    tool_name: str
    status: str
    input_params: ToolInputParams | None
    output_summary: dict[str, str | int | float | bool | None] | None
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class SqlAuditRead(BaseModel):
    """SQL 审计历史的透明投影，不包含连接、堆栈或宿主机路径。"""

    id: str
    run_id: str | None
    tool_call_id: str | None
    datasource_id: str | None
    datasource_deleted: bool = False
    schema_revision: int | None = Field(default=None, ge=0)
    connection_revision: int = Field(default=0, ge=0)
    attempt_no: int = Field(ge=0)
    repaired_from_id: str | None = Field(default=None, max_length=120)
    original_sql: str
    normalized_sql: str | None
    status: str
    statement_type: str | None
    referenced_tables: list[str] = Field(default_factory=list)
    blocked_reason_code: str | None
    blocked_reason: str | None
    artifact_id: str | None
    row_count: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class RunArtifactRead(BaseModel):
    """属于某个 Run 的 Artifact 元数据，不暴露受控存储路径。"""

    id: str
    run_id: str | None
    session_id: str | None
    tool_call_id: str | None
    datasource_deleted: bool = False
    type: ArtifactType
    title: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    inline_previewable: bool
    preview: dict[str, object] | None
    metadata: dict[str, object] | None
    content_hash: str
    created_at: datetime


class ModelRuntimeSnapshot(BaseModel):
    """一次 Run 使用的模型非敏感参数，不能包含 API Key 或任何 Secret。"""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=120)
    provider: Literal["openai-compatible"]
    model_name: str = Field(min_length=1, max_length=120)
    base_url: AnyHttpUrl
    temperature: float = Field(ge=0, le=2)
    run_timeout_seconds: int = Field(ge=1, le=600)
    final_output_mode: FinalOutputMode
    model_capability_fingerprint: str = Field(min_length=64, max_length=128)
    context_window_tokens: int = Field(default=32_768, ge=1_024, le=1_000_000)
    context_window_source: Literal["profile", "runtime_fallback"] = "runtime_fallback"
    input_budget_tokens: int = Field(default=1, ge=1)
    output_budget_tokens: int = Field(default=4_096, ge=1)
    system_budget_tokens: int = Field(default=4_096, ge=0)
    tool_schema_cost: int = Field(default=8_192, ge=0)
    safety_margin_tokens: int = Field(default=1_024, ge=0)
