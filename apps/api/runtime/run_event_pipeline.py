"""Run 事件账本：先入库，再通知 SSE。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

from agent_runtime.contracts import AgentErrorCode, AgentFailure, AgentToolName
from contracts.ids import make_id
from contracts.run_events import RunEventCreate, RunEventRead, RunEventType
from contracts.runs import project_tool_input_params
from contracts.validation import DataLinkConsumptionCreate
from metadata.models import RunEventModel, ToolCallModel, utc_now
from metadata.repositories import RunDatalinkConsumptionRepository
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_EVENT_PAYLOAD_BYTES: Final = 8_192
_MAX_AGENT_TURN_EVENT_PAYLOAD_BYTES: Final = 64 * 1_024
logger = logging.getLogger(__name__)
_Notifier = Callable[[str], Awaitable[None]]
_sequence_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# 正文事件沿用 DataFoundry 的做法：事件账本保存模型已经产出的正文，
# 不再用一条 ASCII 白名单拦截中文、Markdown 或正常换行。内部标识仍需收紧，
# 避免把任意正文混进 tool/artifact/error 等结构字段。
_SAFE_EVENT_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_DIAGNOSTIC_LIST = re.compile(r"^[A-Za-z0-9_.\[\],:;-]{1,1000}$")
_MAX_EVENT_TEXT_LENGTH: Final = 4_000
_MAX_AGENT_TURN_TEXT_LENGTH: Final = 16_000
_MAX_TOOL_FAILURE_TEXT_LENGTH: Final = 500
_PREPARATION_PHASES: Final = frozenset({"run_opening", "semantic_context", "analysis_plan"})
_PREPARATION_STATUSES: Final = frozenset({"completed", "timed_out", "cancelled", "failed"})
_BLOCK_REASONS: Final = frozenset(
    {
        "required_data_unavailable",
        "operation_not_allowed",
        "capability_unavailable",
        "user_constraint_conflict",
    }
)
_AGENT_TURN_STATUSES: Final = frozenset({"completed", "failed", "cancelled"})
_AGENT_TURN_ACTION_KINDS: Final = frozenset({"tool_call", "respond", "model_error"})
_AGENT_TURN_TOOL_NAMES: Final = frozenset(
    tool.value for tool in AgentToolName if tool is not AgentToolName.UNKNOWN
)
_FINAL_ANSWER_MODES: Final = frozenset({"markdown", "json_schema", "json_object", "submit_answer"})
_FINAL_ANSWER_FAILURE_CODES: Final = frozenset(
    {
        "MODEL_OUTPUT_INVALID",
        "FINAL_ANSWER_TIMEOUT",
        "FINAL_ANSWER_FACT_MISMATCH",
        "CONTEXT_BUDGET_EXHAUSTED",
    }
)
_FINAL_ANSWER_VALIDATION_STAGES: Final = frozenset({"dto", "markdown", "budget"})
_ANSWER_DATA_FRESHNESS: Final = frozenset(
    {
        "not_queried",
        "current_schema",
        "current_run_observation_only",
        "current_run_evidence",
    }
)
_INTERNAL_EVENT_FIELDS: Final = frozenset(
    {
        "tool_call_id",
        "tool_name",
        "audit_log_id",
        "error_code",
        "artifact_id",
        "artifact_type",
        "assistant_message_id",
        "completion_kind",
        "incomplete_reason",
        "answer_format",
        "reason_code",
        "failure_code",
        "subject",
        "output_summary_json",
        "estimate_source",
    }
)
_EVENT_FIELDS: dict[RunEventType, frozenset[str]] = {
    RunEventType.RUN_QUEUED: frozenset(),
    RunEventType.RUN_STARTED: frozenset(),
    RunEventType.RUN_PREPARATION_STARTED: frozenset({"phase"}),
    RunEventType.RUN_PREPARATION_COMPLETED: frozenset(
        {
            "phase",
            "elapsed_ms",
            "status",
            "failure_code",
            "opening_model_calls",
            "opening_repair_calls",
            "validation_issue_count",
            "validation_issue_types",
            "validation_issue_reasons",
            "validation_issue_paths",
            "validation_tool_call_count",
            "validation_invalid_tool_call_count",
        }
    ),
    RunEventType.RUN_PROTOCOL_SELECTED: frozenset(
        {"protocol_id", "selection_mode", "planning_mode"}
    ),
    RunEventType.ANALYSIS_CLARIFICATION_REQUESTED: frozenset({"reason", "requirement_count"}),
    RunEventType.ANALYSIS_REQUIREMENT_BLOCKED: frozenset({"requirement_id", "reason_code"}),
    RunEventType.ANALYSIS_DISCOVERY_OBSERVED: frozenset(
        {
            "tool_call_id",
            "tool_name",
            "turn_no",
            "audit_log_id",
            "artifact_id",
            "column_count",
            "row_count",
            "rows_truncated",
        }
    ),
    RunEventType.AGENT_TURN_STARTED: frozenset({"turn_no"}),
    RunEventType.AGENT_TURN_COMPLETED: frozenset(
        {
            "turn_no",
            "elapsed_ms",
            "status",
            "action_kind",
            "tool_names",
            "tool_call_count",
            "failure_code",
            "reason_code",
            "reasoning",
            "assistant_output",
            "context_retry_count",
            "context_compaction_count",
            "working_set_count",
            "working_set_compacted",
            "model_input_chars",
            "model_input_tokens",
            "estimated_total_tokens",
            "context_window_tokens",
            "input_budget_tokens",
            "remaining_tokens",
            "estimate_source",
            "working_set_value_count",
        }
    ),
    RunEventType.FINAL_ANSWER_REQUEST_STARTED: frozenset({"attempt", "mode"}),
    RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED: frozenset({"attempt", "mode", "elapsed_ms"}),
    RunEventType.FINAL_ANSWER_VALIDATION_FAILED: frozenset(
        {"attempt", "mode", "elapsed_ms", "failure_code", "validation_stage"}
    ),
    RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT: frozenset(
        {"attempt", "mode", "elapsed_ms", "failure_code"}
    ),
    RunEventType.TOOL_CALLED: frozenset({"tool_call_id", "tool_name", "turn_no"}),
    RunEventType.TOOL_SUCCEEDED: frozenset(
        {
            "tool_call_id",
            "tool_name",
            "turn_no",
            "elapsed_ms",
            "evidence_count",
            "output_summary_json",
        }
    ),
    RunEventType.TOOL_FAILED: frozenset(
        {
            "tool_call_id",
            "tool_name",
            "turn_no",
            "elapsed_ms",
            "output_summary_json",
            "error_code",
            "reason_code",
            "error_message",
            "hint",
            "retryable",
            "subject",
            "line",
            "column",
        }
    ),
    RunEventType.ARTIFACT_CREATED: frozenset({"artifact_id", "artifact_type"}),
    RunEventType.ANSWER_DELTA: frozenset({"delta"}),
    RunEventType.ANSWER_READY: frozenset(
        {
            "assistant_message_id",
            "artifact_count",
            "evidence_count",
            "answer_format",
            "completion_kind",
            "incomplete_reason",
            "claim_audit_summary_json",
            "claim_audit_truncated",
            "answer_data_freshness",
            "historical_context_injected",
            "historical_summary_count",
        }
    ),
    RunEventType.RUN_CANCEL_REQUESTED: frozenset({"reason_code"}),
    RunEventType.RUN_SUCCEEDED: frozenset(),
    RunEventType.RUN_FAILED: frozenset({"error_code", "error_message"}),
    RunEventType.RUN_CANCELED: frozenset({"error_code", "error_message"}),
}

# Current protocol minimums.  Payloads from the removed preparation/event
# protocol are rejected instead of being repaired or silently projected.
_EVENT_REQUIRED_FIELDS: dict[RunEventType, frozenset[str]] = {
    RunEventType.RUN_QUEUED: frozenset(),
    RunEventType.RUN_STARTED: frozenset(),
    RunEventType.RUN_PREPARATION_STARTED: frozenset({"phase"}),
    RunEventType.RUN_PREPARATION_COMPLETED: frozenset({"phase", "elapsed_ms", "status"}),
    RunEventType.RUN_PROTOCOL_SELECTED: frozenset({"protocol_id", "selection_mode"}),
    RunEventType.ANALYSIS_CLARIFICATION_REQUESTED: frozenset({"reason", "requirement_count"}),
    RunEventType.ANALYSIS_REQUIREMENT_BLOCKED: frozenset({"requirement_id", "reason_code"}),
    RunEventType.ANALYSIS_DISCOVERY_OBSERVED: frozenset(
        {
            "tool_call_id",
            "tool_name",
            "turn_no",
            "audit_log_id",
            "artifact_id",
            "column_count",
            "row_count",
            "rows_truncated",
        }
    ),
    RunEventType.AGENT_TURN_STARTED: frozenset({"turn_no"}),
    RunEventType.AGENT_TURN_COMPLETED: frozenset(
        {
            "turn_no",
            "elapsed_ms",
            "status",
            "action_kind",
            "tool_names",
            "tool_call_count",
        }
    ),
    RunEventType.FINAL_ANSWER_REQUEST_STARTED: frozenset({"attempt", "mode"}),
    RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED: frozenset({"attempt", "mode", "elapsed_ms"}),
    RunEventType.FINAL_ANSWER_VALIDATION_FAILED: frozenset(
        {"attempt", "mode", "elapsed_ms", "failure_code", "validation_stage"}
    ),
    RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT: frozenset(
        {"attempt", "mode", "elapsed_ms", "failure_code"}
    ),
    RunEventType.TOOL_CALLED: frozenset({"tool_call_id", "tool_name", "turn_no"}),
    RunEventType.TOOL_SUCCEEDED: frozenset(
        {
            "tool_call_id",
            "tool_name",
            "turn_no",
            "elapsed_ms",
            "evidence_count",
            "output_summary_json",
        }
    ),
    RunEventType.TOOL_FAILED: frozenset(
        {
            "tool_call_id",
            "tool_name",
            "turn_no",
            "elapsed_ms",
            "output_summary_json",
            "error_code",
        }
    ),
    RunEventType.ARTIFACT_CREATED: frozenset({"artifact_id", "artifact_type"}),
    RunEventType.ANSWER_DELTA: frozenset({"delta"}),
    RunEventType.ANSWER_READY: frozenset(
        {
            "assistant_message_id",
            "artifact_count",
            "evidence_count",
            "answer_format",
            "completion_kind",
            "claim_audit_summary_json",
            "claim_audit_truncated",
            "answer_data_freshness",
            "historical_context_injected",
            "historical_summary_count",
        }
    ),
    RunEventType.RUN_CANCEL_REQUESTED: frozenset({"reason_code"}),
    RunEventType.RUN_SUCCEEDED: frozenset(),
    RunEventType.RUN_FAILED: frozenset({"error_code", "error_message"}),
    RunEventType.RUN_CANCELED: frozenset({"error_code", "error_message"}),
}


class RunEventPipeline:
    """同一 Run 的 seq 只在这里分配，历史读取不触发任何外部调用。"""

    def __init__(self, db: AsyncSession, *, notify: _Notifier | None = None) -> None:
        self._db = db
        self._notify = notify

    @staticmethod
    @asynccontextmanager
    async def transaction(run_id: str):
        async with _sequence_locks[run_id]:
            yield

    async def append(self, event: RunEventCreate) -> RunEventRead:
        try:
            _validate_payload(event.type, event.payload)
        except ValueError:
            # 只记录定位事件所需的稳定字段，不把正文、Prompt 或密钥写进日志。
            logger.warning(
                "Run event payload rejected",
                extra={
                    "run_id": event.run_id,
                    "event_type": event.type.value,
                    "error_code": "RUN_EVENT_PAYLOAD_INVALID",
                },
            )
            raise
        sequence = (
            int(
                await self._db.scalar(
                    select(func.coalesce(func.max(RunEventModel.seq), 0)).where(
                        RunEventModel.run_id == event.run_id
                    )
                )
                or 0
            )
            + 1
        )
        model = RunEventModel(
            id=make_id("event"),
            run_id=event.run_id,
            seq=sequence,
            event_type=event.type.value,
            payload_json=event.payload,
        )
        self._db.add(model)
        await self._db.flush()
        await self._apply_projection(event)
        return _to_read(model)

    async def publish(self, event: RunEventCreate) -> AgentFailure | None:
        """将一次已构造的事件持久化并在提交后唤醒订阅者。"""

        async with self.transaction(event.run_id):
            await self.append(event)
            await self._db.commit()
        await self.notify_committed(event.run_id)
        return None

    async def notify_committed(self, run_id: str) -> None:
        if self._notify is not None:
            await self._notify(run_id)

    async def record_datalink_consumption(self, payload: DataLinkConsumptionCreate) -> None:
        """Save the safe semantic payload in the current event transaction."""

        await RunDatalinkConsumptionRepository(self._db).create(payload)

    async def list_after(
        self, run_id: str, *, after_seq: int, limit: int = 200
    ) -> list[RunEventRead]:
        result = await self._db.scalars(
            select(RunEventModel)
            .where(RunEventModel.run_id == run_id, RunEventModel.seq > after_seq)
            .order_by(RunEventModel.seq.asc())
            .limit(limit)
        )
        return [_to_read(model) for model in result]

    async def finalize_pending_projections(self, run_id: str, *, error_code: str) -> None:
        """终态前把仍未结束的工具调用标成失败，历史不会留下 running。"""

        tools = list(
            await self._db.scalars(
                select(ToolCallModel).where(
                    ToolCallModel.run_id == run_id,
                    ToolCallModel.finished_at.is_(None),
                )
            )
        )
        now = utc_now()
        for tool in tools:
            tool.status = "failed"
            tool.error_code = error_code
            tool.finished_at = now

    async def _apply_projection(self, event: RunEventCreate) -> None:
        if event.type is RunEventType.TOOL_CALLED:
            tool_id = _string(event.payload, "tool_call_id")
            existing_scoped = await self._db.scalar(
                select(ToolCallModel)
                .where(
                    ToolCallModel.run_id == event.run_id,
                    ToolCallModel.tool_call_id == tool_id,
                    ToolCallModel.finished_at.is_(None),
                )
                .order_by(ToolCallModel.started_at.desc(), ToolCallModel.id.desc())
            )
            if existing_scoped is None:
                tool_name = _string(event.payload, "tool_name")
                internal_id = tool_id
                if await self._db.get(ToolCallModel, internal_id) is not None:
                    internal_id = make_id("tool")
                self._db.add(
                    ToolCallModel(
                        id=internal_id,
                        run_id=event.run_id,
                        tool_call_id=tool_id,
                        tool_name=tool_name,
                        input_json=project_tool_input_params(tool_name, event.tool_input),
                    )
                )
                await self._db.flush()
        elif event.type in {RunEventType.TOOL_SUCCEEDED, RunEventType.TOOL_FAILED}:
            tool_id = _string(event.payload, "tool_call_id")
            tool = await self._find_tool_call(event.run_id, tool_id)
            if tool is None:
                raise ValueError("工具事件引用了不存在的 ToolCall")
            if tool.finished_at is None:
                tool.finished_at = utc_now()
                tool.status = "succeeded" if event.type is RunEventType.TOOL_SUCCEEDED else "failed"
                if event.type is RunEventType.TOOL_SUCCEEDED:
                    summary_json = event.payload.get("output_summary_json")
                    if isinstance(summary_json, str):
                        tool.output_summary_json = _parse_output_summary_json(summary_json)
                if event.type is RunEventType.TOOL_FAILED:
                    tool.error_code = _string(event.payload, "error_code")
                    error_message = event.payload.get("error_message")
                    if isinstance(error_message, str):
                        tool.error_message = error_message
                    summary_json = event.payload.get("output_summary_json")
                    if isinstance(summary_json, str):
                        tool.output_summary_json = _parse_output_summary_json(summary_json)

    async def _find_tool_call(self, run_id: str, tool_call_id: str) -> ToolCallModel | None:
        """按 Run 和模型原始编号找尚未结束的调用。"""

        tool = await self._db.scalar(
            select(ToolCallModel)
            .where(
                ToolCallModel.run_id == run_id,
                ToolCallModel.tool_call_id == tool_call_id,
                ToolCallModel.finished_at.is_(None),
            )
            .order_by(ToolCallModel.started_at.desc(), ToolCallModel.id.desc())
        )
        return tool


class RunEventNotifier:
    """仅负责唤醒 SSE，不保存事件正文。"""

    def __init__(self) -> None:
        self._conditions: defaultdict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def notify(self, run_id: str) -> None:
        condition = self._conditions[run_id]
        async with condition:
            condition.notify_all()

    @asynccontextmanager
    async def subscription(self, run_id: str):
        condition = self._conditions[run_id]
        async with condition:
            yield _RunEventSubscription(condition)


class _RunEventSubscription:
    def __init__(self, condition: asyncio.Condition) -> None:
        self._condition = condition

    async def wait(self, *, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._condition.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True


def _validate_payload(event_type: RunEventType, payload: dict[str, object]) -> None:
    allowed = _EVENT_FIELDS[event_type]
    if set(payload).difference(allowed):
        raise ValueError("Run 事件 payload 包含未允许字段")
    missing = _EVENT_REQUIRED_FIELDS[event_type].difference(payload)
    if missing:
        raise ValueError(f"Run 事件 payload 缺少当前协议字段: {sorted(missing)}")
    for key, value in payload.items():
        if key == "attempt":
            if (
                event_type
                in {
                    RunEventType.FINAL_ANSWER_REQUEST_STARTED,
                    RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
                    RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
                    RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
                }
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 1
            ):
                continue
            raise ValueError("最终答案事件尝试序号无效")
        if key == "turn_no":
            if (
                event_type
                in {
                    RunEventType.AGENT_TURN_STARTED,
                    RunEventType.AGENT_TURN_COMPLETED,
                    RunEventType.TOOL_CALLED,
                    RunEventType.TOOL_SUCCEEDED,
                    RunEventType.TOOL_FAILED,
                    RunEventType.ANALYSIS_DISCOVERY_OBSERVED,
                }
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 10_000
            ):
                continue
            raise ValueError("Agent 回合序号无效")
        if key == "opening_model_calls":
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and payload.get("phase") == "run_opening"
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 2
            ):
                continue
            raise ValueError("Opening 模型调用次数无效")
        if key == "opening_repair_calls":
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and payload.get("phase") == "run_opening"
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 1
            ):
                continue
            raise ValueError("Opening 修复调用次数无效")
        if key == "validation_issue_count":
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 12
            ):
                continue
            raise ValueError("准备阶段诊断数量无效")
        if key in {"validation_tool_call_count", "validation_invalid_tool_call_count"}:
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 64
            ):
                continue
            raise ValueError("准备阶段工具调用计数无效")
        if key in {
            "validation_issue_types",
            "validation_issue_reasons",
            "validation_issue_paths",
        }:
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and isinstance(value, str)
                and _SAFE_DIAGNOSTIC_LIST.fullmatch(value) is not None
            ):
                continue
            raise ValueError("准备阶段诊断索引无效")
        if key == "mode":
            if (
                event_type
                in {
                    RunEventType.FINAL_ANSWER_REQUEST_STARTED,
                    RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
                    RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
                    RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
                }
                and isinstance(value, str)
                and value in _FINAL_ANSWER_MODES
            ):
                continue
            raise ValueError("最终答案事件输出模式无效")
        if key == "failure_code":
            if (
                event_type
                in {
                    RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
                    RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
                }
                and isinstance(value, str)
                and value in _FINAL_ANSWER_FAILURE_CODES
            ):
                continue
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and isinstance(value, str)
                and value in {code.value for code in AgentErrorCode}
            ):
                continue
            if (
                event_type is RunEventType.AGENT_TURN_COMPLETED
                and isinstance(value, str)
                and value in {code.value for code in AgentErrorCode}
            ):
                continue
            raise ValueError("最终答案事件失败分类无效")
        if key == "validation_stage":
            if (
                event_type is RunEventType.FINAL_ANSWER_VALIDATION_FAILED
                and isinstance(value, str)
                and value in _FINAL_ANSWER_VALIDATION_STAGES
            ):
                continue
            raise ValueError("最终答案校验阶段无效")
        if key == "answer_data_freshness":
            if event_type is RunEventType.ANSWER_READY and value in _ANSWER_DATA_FRESHNESS:
                continue
            raise ValueError("答案数据来源状态无效")
        if key == "phase":
            if (
                event_type
                in {
                    RunEventType.RUN_PREPARATION_STARTED,
                    RunEventType.RUN_PREPARATION_COMPLETED,
                }
                and isinstance(value, str)
                and value in _PREPARATION_PHASES
            ):
                continue
            raise ValueError("Run 事件准备阶段无效")
        if key == "protocol_id":
            if event_type is RunEventType.RUN_PROTOCOL_SELECTED and value in {
                "general-task",
                "data-analysis",
            }:
                continue
            raise ValueError("Run 协议标识无效")
        if key == "selection_mode":
            if event_type is RunEventType.RUN_PROTOCOL_SELECTED and value in {
                "opening",
                "replayed",
            }:
                continue
            raise ValueError("Run 协议选择方式无效")
        if key == "planning_mode":
            if event_type is RunEventType.RUN_PROTOCOL_SELECTED and value in {
                "ready",
                "discovery",
                "needs_semantic_context",
                "clarification",
            }:
                continue
            raise ValueError("Run 计划模式无效")
        if key == "status":
            if (
                event_type is RunEventType.RUN_PREPARATION_COMPLETED
                and isinstance(value, str)
                and value in _PREPARATION_STATUSES
            ):
                continue
            if (
                event_type is RunEventType.AGENT_TURN_COMPLETED
                and isinstance(value, str)
                and value in _AGENT_TURN_STATUSES
            ):
                continue
            raise ValueError("Run 事件准备阶段状态无效")
        if key == "action_kind":
            if (
                event_type is RunEventType.AGENT_TURN_COMPLETED
                and isinstance(value, str)
                and value in _AGENT_TURN_ACTION_KINDS
            ):
                continue
            raise ValueError("Agent 回合动作类型无效")
        if key == "tool_names":
            if event_type is RunEventType.AGENT_TURN_COMPLETED and isinstance(value, str):
                names = value.split(",") if value else []
                if (
                    len(names) <= 8
                    and len(names) == len(set(names))
                    and all(name in _AGENT_TURN_TOOL_NAMES for name in names)
                ):
                    continue
            raise ValueError("Agent 回合工具摘要无效")
        if key == "reason":
            if (
                event_type is RunEventType.ANALYSIS_CLARIFICATION_REQUESTED
                and value == "scope_ambiguous"
            ):
                continue
            raise ValueError("Run 事件澄清原因无效")
        if key == "requirement_count":
            if (
                event_type is RunEventType.ANALYSIS_CLARIFICATION_REQUESTED
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 16
            ):
                continue
            raise ValueError("Run 事件澄清目标数量无效")
        if key == "requirement_id":
            if (
                event_type is RunEventType.ANALYSIS_REQUIREMENT_BLOCKED
                and isinstance(value, str)
                and re.fullmatch(r"R[1-9][0-9]{0,2}", value)
            ):
                continue
            raise ValueError("受阻目标标识无效")
        if key == "reason_code":
            if event_type is RunEventType.ANALYSIS_REQUIREMENT_BLOCKED and value in _BLOCK_REASONS:
                continue
            if isinstance(value, str) and _SAFE_EVENT_IDENTIFIER.fullmatch(value):
                continue
            raise ValueError("Run 事件原因分类无效")
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            continue
        if isinstance(value, str):
            if event_type is RunEventType.AGENT_TURN_COMPLETED and key in {
                "reasoning",
                "assistant_output",
            }:
                _validate_agent_turn_text(value)
                continue
            if event_type is RunEventType.ANSWER_READY and key == "claim_audit_summary_json":
                _parse_claim_audit_summary_json(value)
                continue
            if (
                event_type in {RunEventType.TOOL_SUCCEEDED, RunEventType.TOOL_FAILED}
                and key == "output_summary_json"
            ):
                _parse_output_summary_json(value)
                continue
            if event_type is RunEventType.ANSWER_DELTA and key == "delta":
                _validate_answer_delta(value)
                continue
            if event_type is RunEventType.TOOL_FAILED and key in {
                "error_message",
                "hint",
                "subject",
            }:
                _validate_tool_failure_text(value)
                continue
            if (
                event_type in {RunEventType.RUN_FAILED, RunEventType.RUN_CANCELED}
                and key == "error_message"
            ):
                if 0 < len(value) <= 500:
                    continue
                raise ValueError("Run 终态错误说明无效")
            if key in _INTERNAL_EVENT_FIELDS and _SAFE_EVENT_IDENTIFIER.fullmatch(value):
                continue
        raise ValueError("Run 事件 payload 必须是有限安全值")
    if event_type is RunEventType.RUN_PREPARATION_COMPLETED:
        opening_calls = payload.get("opening_model_calls")
        repair_calls = payload.get("opening_repair_calls")
        if (opening_calls is None) != (repair_calls is None):
            raise ValueError("Opening 调用计数必须成对出现")
        if isinstance(opening_calls, int) and isinstance(repair_calls, int):
            if repair_calls > opening_calls - 1:
                raise ValueError("Opening 修复调用次数超过总调用次数")
    if event_type is RunEventType.ANALYSIS_DISCOVERY_OBSERVED:
        if payload.get("tool_name") != AgentToolName.SQL.value:
            raise ValueError("Discovery 观察只能关联只读 SQL 工具")
        if not isinstance(payload.get("rows_truncated"), bool):
            raise ValueError("Discovery 观察截断标志无效")
        for key in ("column_count", "row_count"):
            value = payload.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("Discovery 观察规模摘要无效")
        for key in ("tool_call_id", "audit_log_id", "artifact_id"):
            value = payload.get(key)
            if not isinstance(value, str) or _SAFE_EVENT_IDENTIFIER.fullmatch(value) is None:
                raise ValueError("Discovery 观察引用无效")
    max_payload_bytes = (
        _MAX_AGENT_TURN_EVENT_PAYLOAD_BYTES
        if event_type is RunEventType.AGENT_TURN_COMPLETED
        else _MAX_EVENT_PAYLOAD_BYTES
    )
    if (
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        > max_payload_bytes
    ):
        raise ValueError("Run 事件 payload 超过安全大小限制")


def _parse_output_summary_json(value: str) -> dict[str, str | int | float | bool | None]:
    """校验工具摘要 JSON，只允许有限标量，拒绝正文或嵌套结果。"""

    if len(value) > _MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("工具摘要超过安全大小限制")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("工具摘要不是合法 JSON") from exc
    if not isinstance(parsed, dict) or len(parsed) > 20:
        raise ValueError("工具摘要必须是有限对象")
    safe: dict[str, str | int | float | bool | None] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", key):
            raise ValueError("工具摘要字段名不安全")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("工具摘要只允许标量")
        if isinstance(item, str) and len(item) > _MAX_EVENT_TEXT_LENGTH:
            raise ValueError("工具摘要文本过长")
        safe[key] = item
    return safe


def _parse_claim_audit_summary_json(value: str) -> list[dict[str, object]]:
    """校验答案事件中的有界 Claim 摘要，不接受原始结果或嵌套正文。"""

    if len(value) > _MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("Claim 审计摘要超过安全大小限制")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Claim 审计摘要不是合法 JSON") from exc
    if not isinstance(parsed, list) or len(parsed) > 16:
        raise ValueError("Claim 审计摘要必须是有限数组")
    for item in parsed:
        if not isinstance(item, dict) or len(item) > 8:
            raise ValueError("Claim 审计摘要项必须是有限对象")
        for key, nested in item.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", key):
                raise ValueError("Claim 审计摘要字段名不安全")
            if key == "facts":
                if not isinstance(nested, list) or len(nested) > 64:
                    raise ValueError("Claim 审计事实必须是有限数组")
                for fact in nested:
                    if not isinstance(fact, dict) or len(fact) > 6:
                        raise ValueError("Claim 审计事实必须是有限对象")
                    for fact_key, fact_value in fact.items():
                        if fact_key == "dimensions":
                            if not isinstance(fact_value, dict) or len(fact_value) > 16:
                                raise ValueError("Claim 审计维度必须是有限对象")
                            if any(
                                isinstance(dimension_value, (dict, list))
                                or not isinstance(dimension_value, (str, int, float, bool))
                                for dimension_value in fact_value.values()
                            ):
                                raise ValueError("Claim 审计维度只允许标量")
                        elif isinstance(fact_value, (dict, list)):
                            raise ValueError("Claim 审计事实只允许标量")
            elif isinstance(nested, (dict, list)):
                raise ValueError("Claim 审计摘要只允许事实数组和标量")
            elif not isinstance(nested, (str, int, float, bool)) and nested is not None:
                raise ValueError("Claim 审计摘要只允许有限标量")
    return parsed


def _validate_answer_delta(value: str) -> None:
    """只拦截空正文、控制字符和过长分片，不限制语言或 Markdown 写法。"""

    if not value or len(value) > _MAX_EVENT_TEXT_LENGTH:
        raise ValueError("answer.delta 正文为空或超过长度限制")
    if any(
        unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
        for character in value
    ):
        raise ValueError("answer.delta 正文包含控制字符")


def _validate_agent_turn_text(value: str) -> None:
    """允许内部排查使用模型实际返回的回合文本，但仍限制单字段大小。"""

    if not value or len(value) > _MAX_AGENT_TURN_TEXT_LENGTH:
        raise ValueError("Agent 回合文本为空或超过长度限制")
    if any(
        unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
        for character in value
    ):
        raise ValueError("Agent 回合文本包含控制字符")


def _validate_tool_failure_text(value: str) -> None:
    """失败说明只允许有限的人话摘要，不允许把原始异常正文塞入事件。"""

    if not value or len(value) > _MAX_TOOL_FAILURE_TEXT_LENGTH:
        raise ValueError("工具失败说明为空或超过长度限制")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("工具失败说明包含控制字符")


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Run 事件缺少 {key}")
    return value


def _to_read(model: RunEventModel) -> RunEventRead:
    return RunEventRead(
        run_id=model.run_id,
        seq=model.seq,
        type=RunEventType(model.event_type),
        timestamp=model.created_at,
        payload=model.payload_json,
    )
