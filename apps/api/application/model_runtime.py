"""将固定 Run 的 OpenAI-compatible 配置适配为 LangChain 原生调用。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    AnalysisPlanFinalizationArguments,
    AnalysisPlanFinalizationRequest,
    AnalysisPlanInvalidFailure,
    AnalysisPlanningDraft,
    DiscoveryObservedFollowup,
    FinalMarkdownPayload,
    OpeningContextProjection,
    OpeningValidationIssue,
    RunOpeningAttemptSummary,
    RunOpeningDecision,
    RunOpeningInvalidFailure,
    SemanticDegradedFollowup,
    SemanticResolvedFollowup,
    StartDataAnalysisArguments,
)
from agent_runtime.conversation_context import (
    discovery_observation_projection,
    discovery_schema_index,
    opening_repair_projection,
    plan_finalization_projection,
    plan_schema_dependencies,
)
from agent_runtime.ports import CancellationSignal
from agent_runtime.token_estimate import estimate_message_tokens
from contracts.datasources import SchemaSummaryRead
from contracts.model_profiles import FinalOutputMode
from contracts.runs import ModelRuntimeSnapshot
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    convert_to_messages,
)
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from application.runtime_limits import RuntimeLimits
from application.runtime_wait import (
    RuntimeWaitCanceled,
    RuntimeWaitTimedOut,
    await_runtime_call,
)

_SUBMIT_ANSWER_TOOL_NAME = "submit_answer"
_ANALYSIS_COMMIT_TOOL_NAME = "commit_analysis_claims"
_ANALYSIS_RESERVE_RATIO = 0.30
_FINAL_RESERVE_RATIO = 0.15
_FINAL_ANSWER_CLEANUP_MARGIN_SECONDS = 5.0
_MAX_SINGLE_ENCODED_PLAN_CHARS = 64_000
_REASONING_COMPATIBLE_HOSTS = {"api.deepseek.com", "api.siliconflow.cn"}
logger = logging.getLogger(__name__)


_OPENING_SYSTEM_PROMPT = (
    "你是 DataPilot Run Opening。先且只先判断回答是否必须读取当前数据。"
    "若不需要当前数据，只问通用定义、原因、原理或方法，直接输出正常答案，"
    "不输出分类标签、不调用工具。若需要当前数据，必须只返回一个无正文的"
    "start_data_analysis ToolCall，不能因计划较难而放弃调用。\n"
    "数据任务以继续执行为首要目标。plan.mode 只能是 ready、discovery、"
    "needs_semantic_context 或 clarification。"
    "窄任务只有在目标、完成条件和最小证据已明确时使用 ready。"
    "宽泛、多个指标、多步骤或正式报告任务可以先使用 discovery，但 discovery 不是随便浏览："
    "discovery_scope 必须覆盖用户已经明确提到的对象所需的最小完整范围，包括必要表、字段和连接键，"
    "不能提交空 scope，也不能为了缩短 JSON 随意删除依赖。"
    "Schema 已能点名分析对象、但目标或指标仍宽泛时使用 discovery，"
    "不得 clarification，也不得用 ready 发明默认指标。"
    "后续定稿器会依据原问题和受限观察生成正式 ready 计划。"
    "只有用户范围、指标、比较基线或交付格式缺失，"
    "且不能形成最小目标时才使用 clarification。"
    "‘找出异常’没有明确对象、指标或异常口径，必须 clarification；"
    "不得把无法判断的业务含义猜成默认指标。\n"
    "仅当存在语义缺口时使用 needs_semantic_context："
    "用户用语无法对应到当前 Schema 的表、字段或连接键。"
    "提到 datalink、图谱或「用上」这类过程要求不是语义缺口。"
    "semantic_request 只属于 needs_semantic_context，必须含 query（可选 focus）；"
    "其它 mode 必须省略 semantic_request 键，禁止填 null。"
    "图谱检索留给后续 explore_datalink（若该工具开放）。\n"
    "Opening 不做 SQL 设计、字段校验、结果解释或产物生成，"
    "不预设任何具体业务实体、客户、订单或默认指标；"
    "分析对象只能来自本次用户问题和当前 Schema。‘数据模型是什么？’、‘数据中的偏差可能来自哪里？’"
    "是不依赖当前数据的 general-task；‘当前数据模型是什么？’、‘这份数据中的偏差可能来自哪里？’"
    "需要读取当前数据，属于 data-analysis。‘什么是同比？’是 general-task；"
    "‘解释同比并计算当前数据的同比结果’是 data-analysis。\n"
    "分析请求只能通过一个 start_data_analysis ToolCall 提交，不能同时输出正文。"
    "plan 只写目标、完成条件、模式和必要约束，不写运行 ID、工具 ID、SQL、凭据、数据值或隐藏推理。"
    "ready/discovery 的每个 requirement 都要有 description、acceptance_criteria 和 fulfillment；"
    "只看 Schema 时使用 context_only，需要当前数值时使用 evidence；"
    "表、字段、类型、表间关系、连接键属于结构信息，用 context_only；"
    "只有当前数据中的数值、分组或比较才用 evidence；"
    "clarification 不提交 requirements，只写一个具体澄清问题；"
    "discovery 必须同时提交非空的最小 discovery_scope。Session 背景只用于理解连续"
    "对话，历史 assistant 内容不是当前 Evidence 或 Claim；当前数据问题必须由本 Run 重新读取。"
)


_OPENING_REPAIR_SYSTEM_PROMPT = (
    "你正在修复已经确定为 data-analysis 的无效 Opening 合同。不能退出数据分析协议，"
    "不能输出普通答案；唯一合法输出是空正文加一个完整 start_data_analysis ToolCall。"
    "目标是让任务继续执行，不是复原一份复杂计划。\n"
    "先按 finding 修复原 mode 的最小合同。"
    "如果原 mode=ready 仍需要多个 assertion、多个指标或很长参数，"
    "允许改成 discovery 作为执行桥接；但必须保留用户原目标，"
    "并提交覆盖已明确对象所需的最小完整 discovery_scope，"
    "不能是空 scope。不得把 discovery 改成 ready、clarification 或 general-task；"
    "clarification、needs_semantic_context 也只能保持原 mode。"
    "非语义 mode 若携带 semantic_request，删除该字段并保持原 mode，"
    "禁止升为 needs_semantic_context。"
    "若原 mode 已是 needs_semantic_context，保持该 mode 并按 finding 修正请求。\n"
    "discovery 必须恰好包含目标、完成条件、fulfillment={mode:context_only,sources:[schema]}，"
    "以及非空最小完整"
    "discovery_scope；scope.tables 使用物理表名，scope.columns 每项必须是 table.column，"
    "并包含完成该目标所需的连接字段；不得有 assertion、evidence 或 required_artifacts。"
    "ready 只生成最小 evidence 合同；clarification 只提交一个具体问题且 requirements=[]。"
    "字段错误必须先删除旧的非法字段，再从 qualified_columns 中逐字复制真实存在的 table.column；"
    "unknown_column 或 schema_reference_invalid 不能通过保留旧值、改拼写或补一个裸字段来修复。"
    "discovery_scope.columns 必须整体重建，不能只返回局部补丁。所有物理字段都必须从 "
    "qualified_columns 清单中逐字复制完整的 table.column；不能自行拼接、改名、猜测或填写裸字段。\n"
    "如果 finding 是 artifact_conflict、shape_invalid 等不涉及具体字段的合同错误，"
    "保留首轮计划已经表达的真实表和字段范围，只修改 finding 指向的合同路径；"
    "不要重新猜测客户、订单或状态字段。\n"
    "逐项执行 finding 后重新提交完整 plan，不返回局部补丁、解释、SQL、数据值、内部 ID、"
    "第二个动作或历史 ToolMessage。"
)


@dataclass(frozen=True)
class RequestBudgetEstimate:
    """一次模型请求的安全预算摘要，不包含消息正文。"""

    input_chars: int
    input_tokens: int
    estimated_total_tokens: int
    context_window_tokens: int
    input_budget_tokens: int
    remaining_tokens: int
    estimate_source: str

    def as_metadata(self) -> dict[str, int | str]:
        return {
            "model_input_chars": self.input_chars,
            "model_input_tokens": self.input_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "context_window_tokens": self.context_window_tokens,
            "input_budget_tokens": self.input_budget_tokens,
            "remaining_tokens": self.remaining_tokens,
            "estimate_source": self.estimate_source,
        }


class _SiliconFlowCompatibleChatModel(ChatOpenAI):
    """保留 SiliconFlow 思考模型多轮 Tool Calling 所需的兼容字段。

    ``langchain-openai`` 面向标准 OpenAI 字段，不会保留供应商返回的
    ``reasoning_content``。SiliconFlow 在下一轮 assistant 工具消息中要求该
    字段原样存在；优先保留 LangChain AIMessage 中的真实值，只有上游没有
    返回该字段时才使用兼容占位。
    """

    def _get_request_payload(self, input_, stop=None, **kwargs: Any) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            source_messages = convert_to_messages(input_)
        except (TypeError, ValueError):
            source_messages = []
        for index, message in enumerate(payload.get("messages", [])):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            reasoning_content = None
            if index < len(source_messages):
                source = source_messages[index]
                if isinstance(source, AIMessage):
                    value = source.additional_kwargs.get("reasoning_content")
                    if isinstance(value, str) and value.strip():
                        reasoning_content = value
            message["reasoning_content"] = reasoning_content or " "
        return payload


def build_openai_compatible_chat_model(
    *,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float,
    timeout: float,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """创建兼容模型；仅对已知需要该字段的 SiliconFlow 请求做适配。"""

    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname in _REASONING_COMPATIBLE_HOSTS:
        model_type = _SiliconFlowCompatibleChatModel
    else:
        model_type = ChatOpenAI
    options: dict[str, Any] = {
        "model": model_name,
        "base_url": base_url,
        "api_key": api_key,
        "temperature": temperature,
        "timeout": timeout,
        "max_retries": 0,
    }
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    return model_type(**options)


@tool(_SUBMIT_ANSWER_TOOL_NAME, args_schema=FinalMarkdownPayload)
def submit_answer(markdown: str) -> str:
    """提交一份完整 Markdown；这个工具不读取数据也不写 Run 终态。"""

    del markdown
    return "最终答案已收到，等待 DataPilot 校验证据后发布。"


@tool("start_data_analysis", args_schema=StartDataAnalysisArguments)
def start_data_analysis(plan: StartDataAnalysisArguments) -> str:
    """声明进入数据分析协议；真正的工具注册由 Run Runtime 在校验后完成。

    Opening 只提交目标、完成条件、mode 和必要约束。mode 为 ready、discovery、
    needs_semantic_context 或 clarification。semantic_request 仅在
    needs_semantic_context 时出现且必须含 query；其它 mode 省略该键，禁止 null。
    ready/discovery/needs_semantic_context 的每个 requirement 都要有 description、
    acceptance_criteria 和 fulfillment；fulfillment.mode 只能是 evidence、
    context_only 或 blocked。物理字段使用 table.column。
    discovery 必须同时提交非空的最小 discovery_scope。
    clarification 不提交 requirements，只写一个具体澄清问题。
    """

    del plan
    return "数据分析计划已接收，等待 Runtime 校验。"


@tool("finalize_analysis_plan", args_schema=AnalysisPlanFinalizationArguments)
def finalize_analysis_plan(plan: AnalysisPlanningDraft) -> str:
    """提交语义检索后的最终分析计划；不执行任何数据操作。

    evidence 的 source_tables、result_columns、sql_constraints、result_checks 和
    claim_extractions 必须放在 fulfillment.assertions[*] 内，不能直接放在 fulfillment。
    required_artifacts 可列多个独立交付物，但每项必须包含 kind 和 description。
    """

    del plan
    return "分析计划已定稿，等待 Runtime 校验。"


class RunDeadline:
    """一次 Run 共用总时限，并为准备、分析和最终答案保留最低时间。"""

    def __init__(self, timeout_seconds: int, limits: RuntimeLimits | None = None) -> None:
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("run_timeout_seconds must be between 1 and 600")
        self._limits = limits or RuntimeLimits()
        self._run_timeout_seconds = float(timeout_seconds)
        started_at = time.monotonic()
        self._deadline = started_at + timeout_seconds
        self._preparation_deadline = started_at + min(
            self._limits.preparation_max_seconds,
            timeout_seconds * (1 - _ANALYSIS_RESERVE_RATIO - _FINAL_RESERVE_RATIO),
        )
        self._final_reserve_seconds = min(
            self._limits.final_reserve_seconds,
            timeout_seconds * _FINAL_RESERVE_RATIO,
        )
        self._preparation_stage_deadlines: dict[str, float] = {}

    def remaining_seconds(self) -> float:
        """返回当前 Run 尚可用于模型请求的时间，不会重新计时。"""

        return max(0.0, self._deadline - time.monotonic())

    @property
    def model_max_output_tokens(self) -> int:
        """返回当前 Run 固定的主 Agent 输出上限。"""

        return self._limits.model_max_output_tokens

    @property
    def runtime_limits(self) -> RuntimeLimits:
        """返回本次 Run 固定使用的运行时限额快照。"""

        return self._limits

    def preparation_remaining_seconds(self) -> float:
        """返回准备阶段可用时间，避免辅助调用挤占 SQL 和最终答案时间。"""

        return max(
            0.0,
            min(self.remaining_seconds(), self._preparation_deadline - time.monotonic()),
        )

    def preparation_stage_remaining_seconds(self, stage: str) -> float:
        """为每个准备子阶段保留独立窗口，重试共享同一个窗口。"""

        max_seconds = {
            "run_opening": self._limits.run_opening_timeout_seconds,
            "semantic_context": self._limits.semantic_context_timeout_seconds,
            "analysis_plan_followup": self._limits.analysis_plan_followup_timeout_seconds,
        }.get(stage)
        if max_seconds is None:
            return self.preparation_remaining_seconds()
        stage_deadline = self._preparation_stage_deadlines.get(stage)
        if stage_deadline is None:
            # The configured stage limit is only an upper bound. The outer
            # preparation deadline and the Run deadline below still prevent a
            # stage from consuming time reserved for later work.
            stage_deadline = time.monotonic() + max_seconds
            self._preparation_stage_deadlines[stage] = stage_deadline
        return max(
            0.0,
            min(
                self.remaining_seconds(),
                self._preparation_deadline - time.monotonic(),
                stage_deadline - time.monotonic(),
            ),
        )

    def discovery_plan_followup_remaining_seconds(self) -> float:
        """Discovery 后按当前剩余总预算定稿，并保留正式分析与答案窗口。"""

        remaining = self.remaining_seconds()
        analysis_reserve = remaining * _ANALYSIS_RESERVE_RATIO
        return max(
            0.0,
            min(
                self._limits.analysis_plan_followup_timeout_seconds,
                remaining - analysis_reserve - self._final_reserve_seconds,
            ),
        )

    def agent_remaining_seconds(self) -> float:
        """返回 Agent 模型调用可用时间，并保留最终答案的最低时间。"""

        return max(0.0, self.remaining_seconds() - self._final_reserve_seconds)

    def commit_remaining_seconds(self) -> float:
        """限制只提交结论的模型回合，避免无效工具参数耗尽分析窗口。"""

        return min(self.agent_remaining_seconds(), self._limits.commit_timeout_seconds)

    def agent_turn_remaining_seconds(self) -> float:
        """限制普通 Agent 单轮等待时间，避免单轮占满整个 Run。"""

        return min(self.agent_remaining_seconds(), self._limits.agent_turn_timeout_seconds)

    def final_remaining_seconds(self) -> float:
        """返回最终答案调用可使用的全部剩余时间。"""

        return self.remaining_seconds()

    def final_answer_remaining_seconds(self) -> float:
        """限制单次最终答案请求，保留总 Run 的统一收尾边界。"""

        return max(
            0.0,
            min(
                self.final_remaining_seconds() - _FINAL_ANSWER_CLEANUP_MARGIN_SECONDS,
                self._limits.final_answer_timeout_seconds,
            ),
        )

    def final_answer_first_token_remaining_seconds(self) -> float:
        """流式最终答案等待首个文本分片的窗口。"""

        return self.final_answer_remaining_seconds()

    def final_answer_idle_remaining_seconds(self) -> float:
        """流式最终答案两次文本分片之间允许的最大空闲时间。"""

        return max(
            0.0,
            min(
                self.final_remaining_seconds() - _FINAL_ANSWER_CLEANUP_MARGIN_SECONDS,
                self._limits.final_answer_idle_timeout_seconds,
            ),
        )


class OpenAICompatibleModelClient:
    """只通过 LangChain 的消息、Tool Calling 和结构化输出接口访问模型。"""

    def __init__(
        self,
        *,
        model_name: str,
        base_url: str,
        api_key: str,
        temperature: float,
        deadline: RunDeadline,
        chat_model: Any | None = None,
        model_context: ModelRuntimeSnapshot | None = None,
    ) -> None:
        if not model_name or not base_url or not api_key:
            raise ValueError("model runtime configuration is incomplete")
        self._deadline = deadline
        self._limits = deadline.runtime_limits
        self._model_context = model_context
        # Runtime uses this flag to distinguish a summary that was actually
        # serialized into an Opening request from one merely selected by the
        # Resolver. It is intentionally adapter-local and never model input.
        self.last_opening_historical_summary_count = 0
        self.last_opening_historical_summary_omitted_count = 0
        self.last_request_budget: RequestBudgetEstimate | None = None
        self._model = chat_model or build_openai_compatible_chat_model(
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            timeout=deadline.remaining_seconds(),
            max_tokens=self._deadline.model_max_output_tokens,
        )

    async def open_run(
        self,
        question: str,
        schema: SchemaSummaryRead,
        cancellation: CancellationSignal,
        *,
        opening_context: OpeningContextProjection,
        repair: bool = False,
        repair_issues: Sequence[OpeningValidationIssue] = (),
        repair_plan_skeleton: Mapping[str, object] | None = None,
    ) -> RunOpeningDecision | AgentFailure:
        """用一次原生 action 调用区分普通问答和数据分析，不做关键词路由。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        if not repair:
            self.last_opening_historical_summary_count = 0
            self.last_opening_historical_summary_omitted_count = 0
        repair_issues_json = json.dumps(
            [issue.model_dump(mode="json") for issue in repair_issues],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        repair_hint = (
            f"本次脱敏校验 finding：{repair_issues_json}\n"
            "请逐项修复 finding 指向的路径，并重新提交完整参数；不要只返回局部补丁。\n"
            if repair
            else ""
        )
        prompt = _OPENING_REPAIR_SYSTEM_PROMPT if repair else _OPENING_SYSTEM_PROMPT
        prompt += (
            "\n服务端计划复杂度上限：每个 requirement 最多 "
            f"{self._limits.agent_max_assertions_per_requirement} 个 assertions，单个 Run 最多 "
            f"{self._limits.agent_max_total_assertions} 个 assertions。只保留直接支撑用户目标的"
            "核心检查；不要把可选的数据质量扫描全部设为 required assertion。"
        )
        if repair:
            repair_projection = opening_repair_projection(
                question=question,
                schema=schema,
                validation_issues=list(repair_issues),
                plan_skeleton=dict(
                    repair_plan_skeleton or {"allowed_action": "start_data_analysis", "text": True}
                ),
            )
            opening_payload = json.dumps(
                repair_projection.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            opening_payload = json.dumps(
                opening_context.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        opening_payload = f"{opening_payload}\n{repair_hint}"
        opening_messages = [SystemMessage(content=prompt), HumanMessage(content=opening_payload)]
        if not self._request_fits_budget(opening_messages, [start_data_analysis]):
            if not repair and (
                opening_context.historical_summaries or opening_context.recent_user_turns
            ):
                # C4: preserve the current question, pending state, preference
                # and safe Schema while retiring non-current conversation text.
                omitted_summary_count = len(opening_context.historical_summaries)
                compact_context = opening_context.model_copy(
                    update={"historical_summaries": [], "recent_user_turns": []}
                )
                opening_payload = json.dumps(
                    compact_context.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                opening_payload = f"{opening_payload}\n{repair_hint}"
                opening_messages = [
                    SystemMessage(content=prompt),
                    HumanMessage(content=opening_payload),
                ]
                opening_context = compact_context
                self.last_opening_historical_summary_omitted_count = omitted_summary_count
        if not self._request_fits_budget(opening_messages, [start_data_analysis]):
            return AgentFailure(
                code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                message="Run Opening 请求超过模型上下文预算",
            )
        if not repair:
            self.last_opening_historical_summary_count = len(opening_context.historical_summaries)
        try:
            response = await self._ainvoke(
                self._model.bind_tools(
                    [start_data_analysis], strict=True, parallel_tool_calls=False
                ),
                opening_messages,
                cancellation,
                timeout_seconds=self._deadline.preparation_stage_remaining_seconds("run_opening"),
            )
        except RuntimeWaitCanceled:
            return _cancelled_failure()
        except RuntimeWaitTimedOut:
            return AgentFailure(
                code=AgentErrorCode.RUN_OPENING_TIMEOUT,
                message="Run Opening 模型请求超过阶段时限",
            )
        except Exception as exc:
            if _is_context_length_error(exc):
                return AgentFailure(
                    code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                    message="模型上下文超过供应商限制",
                )
            logger.warning(
                "Run Opening model request failed",
                extra={
                    "error_code": AgentErrorCode.MODEL_REQUEST_FAILED.value,
                    "error_type": type(exc).__name__,
                },
            )
            return _request_failure("Run Opening 模型请求失败")
        if not isinstance(response, AIMessage):
            return _opening_invalid_failure()

        tool_calls = response.tool_calls
        content = _stream_chunk_text(response)
        if tool_calls:
            if len(tool_calls) != 1 or content.strip():
                return _opening_invalid_failure()
            call = tool_calls[0]
            if not isinstance(call, dict) or call.get("name") != "start_data_analysis":
                return _opening_invalid_failure()
            tool_call_id = call.get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                return _opening_invalid_failure()
            raw_arguments = call.get("args")
            try:
                normalized_arguments = _normalize_plan_arguments(raw_arguments)
            except _PlanArgumentsNormalizationError as exc:
                return _opening_invalid_failure(
                    [_plan_normalization_issue(exc)],
                    attempt_summary=_opening_attempt_summary(raw_arguments),
                )
            attempt_summary = _opening_attempt_summary(normalized_arguments)
            try:
                arguments = StartDataAnalysisArguments.model_validate(
                    normalized_arguments, strict=True
                )
                return RunOpeningDecision(protocol_id="data-analysis", plan=arguments.plan)
            except ValidationError as exc:
                return _opening_invalid_failure(
                    _opening_validation_issues(exc, input_value=normalized_arguments),
                    attempt_summary=attempt_summary,
                )
        if not content.strip():
            return _opening_invalid_failure()
        try:
            return RunOpeningDecision(protocol_id="general-task", answer=content.strip())
        except ValidationError:
            return _opening_invalid_failure()

    async def invoke_with_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        cancellation: CancellationSignal,
    ) -> AIMessage | AgentFailure:
        """单独请求数据 Tool Calling，不叠加任何 response_format。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        if not self._request_fits_budget(messages, tools):
            return AgentFailure(
                code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                message="Agent 请求超过模型上下文预算",
            )
        try:
            bound = self._model.bind_tools(list(tools), strict=True, parallel_tool_calls=False)
            commit_only = _is_analysis_commit_only(tools)
            response = await self._ainvoke(
                bound,
                messages,
                cancellation,
                timeout_seconds=(
                    self._deadline.commit_remaining_seconds()
                    if commit_only
                    else self._deadline.agent_turn_remaining_seconds()
                ),
            )
        except RuntimeWaitCanceled:
            return _cancelled_failure()
        except RuntimeWaitTimedOut:
            if commit_only and self._deadline.remaining_seconds() > 0:
                return _analysis_claim_commit_timeout_failure()
            if not commit_only and self._deadline.agent_remaining_seconds() > 0:
                return _analysis_agent_turn_timeout_failure()
            return _analysis_limit_failure()
        except Exception as exc:
            if _is_context_length_error(exc):
                return AgentFailure(
                    code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                    message="模型上下文超过供应商限制",
                    context_retry_allowed=True,
                )
            logger.warning(
                "Agent model tool request failed",
                extra={
                    "error_code": AgentErrorCode.MODEL_REQUEST_FAILED.value,
                    "error_type": type(exc).__name__,
                },
            )
            return _request_failure("模型请求失败")
        if not isinstance(response, AIMessage):
            return _request_failure("模型返回的不是有效消息")
        return response

    async def finalize_analysis_plan(
        self,
        request: AnalysisPlanFinalizationRequest,
        cancellation: CancellationSignal,
    ) -> AnalysisPlanningDraft | AgentFailure:
        """基于一次语义检索或受限探索观察定稿，不允许形成循环。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        if request.initial_plan.mode == "needs_semantic_context":
            if request.semantic_resolution is not None:
                followup = SemanticResolvedFollowup(semantic_context=request.semantic_resolution)
            elif request.semantic_warning is not None:
                followup = SemanticDegradedFollowup(warning=request.semantic_warning)
            else:
                return _invalid_plan_failure()
        elif request.initial_plan.mode == "discovery":
            followup = DiscoveryObservedFollowup(
                observations=discovery_observation_projection(request.discovery_observations)
            )
        else:
            return _invalid_plan_failure()
        required_tables, columns_by_table = plan_schema_dependencies(
            request.physical_schema, request.initial_plan
        )
        schema_projection = None
        if request.initial_plan.mode == "discovery":
            scope = request.initial_plan.discovery_scope
            if scope is not None:
                schema_projection = discovery_schema_index(request.physical_schema, scope)
        projection = plan_finalization_projection(
            question=request.question,
            initial_plan=request.initial_plan,
            schema=request.physical_schema,
            followup_outcome=followup,
            required_tables=required_tables,
            columns_by_table=columns_by_table,
            schema_projection=schema_projection,
        )
        request_payload = {"plan_finalization": projection.model_dump(mode="json")}
        request_json = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
        prompt = (
            "你是 DataPilot 的分析计划定稿器。根据安全 Schema、首版目标和一次 DataLink 结果"
            "或受限 Discovery 观察"
            "形成最终计划。只调用一次 finalize_analysis_plan，不要输出正文、解释或第二个动作。"
            "这是一次执行定稿，不是重新规划：保持首版目标和交付要求，直接提交能继续执行的最小"
            "ready 计划；只有确实无法定义可验证目标时才提交 clarification，不要再次探索或扩展范围。"
            "plan.mode 只能是 ready 或 clarification；不得再次请求 DataLink，不得编造表、字段、"
            "能力、运行 ID、工具 ID 或数据值。SQL 的派生结果列必须声明在 assertion.result_columns，"
            "图表只能使用 required_artifacts，不能创建 chart Claim。"
            "最重要的层级规则：evidence fulfillment 只能有 mode 和 assertions；"
            "source_tables、result_columns、sql_constraints、result_checks、claim_extractions"
            "必须全部放在 fulfillment.assertions[*] 对象内，绝不能直接放在 fulfillment。"
            "每个 assertion 都必须有 description 和至少一个 claim_extractions；"
            "不要把多个 assertion 字段提升到 fulfillment 层。"
            "source_tables 以及 kind=source 的 constraint.table 只能填写物理表名，"
            "不要把字段写进 source_tables。字段型约束（column、filter、time_range、group_by、"
            "aggregate、dimensions、claim_extractions 和 result_checks）必须引用当前 assertion "
            "source_tables 内的物理字段：如果字段名在 required_schema 的多个来源表中出现，"
            "必须使用完整的 table.column（例如 orders.order_id），不能提交跨表有歧义的裸字段；"
            "只有在当前 assertion 来源范围内唯一时才可以使用裸字段。结果列/聚合 alias 是当前 "
            "assertion 自己的派生输出名，保持裸 alias，不要把 alias 当物理字段或跨 assertion 借用。"
            "aggregate 的 column 只能是单个物理字段或 *，function 只能是 COUNT、SUM 或 AVG；"
            "表达式、COUNT DISTINCT、乘法和 CASE 不要放入 aggregate。"
            "filter 的 operator 支持 eq、gt、gte、lt、lte、is_null、is_not_null；NULL 判断必须"
            "使用 is_null/is_not_null 且 value=null，禁止提交 = NULL。每个 requirement 只保留"
            "支撑用户问题的核心 assertions，避免一次计划包含过多独立检查。"
            "请按目标类型选择下面两种最小骨架之一（字段位置不可改变）。"
            "结构、关系、连接键使用 context_only，sources 至少含 schema；"
            "无图谱时 sources 只用 schema："
            '{"plan":{"mode":"ready","requirements":[{'
            '"description":"目标","acceptance_criteria":["完成条件"],'
            '"fulfillment":{"mode":"context_only","sources":["schema"]}'
            '}],"consumes_pending":false,"execution_constraints":{"forbidden_tools":[],'
            '"required_artifacts":[],"forbidden_artifact_kinds":[]}}}。'
            "当前 Run 已有图谱时 sources 必须同时含 schema 和 semantic_context，"
            "即使首版是 discovery、本次定稿不得再把 plan.mode 写成 needs_semantic_context："
            '{"plan":{"mode":"ready","requirements":[{'
            '"description":"目标","acceptance_criteria":["完成条件"],'
            '"fulfillment":{"mode":"context_only","sources":["schema","semantic_context"]}'
            '}],"consumes_pending":false,"execution_constraints":{"forbidden_tools":[],'
            '"required_artifacts":[],"forbidden_artifact_kinds":[]}}}。'
            "数值、分组、比较使用 evidence："
            '{"plan":{"mode":"ready","requirements":[{'
            '"description":"目标","acceptance_criteria":["完成条件"],'
            '"fulfillment":{"mode":"evidence","assertions":[{'
            '"description":"证据","source_tables":["表名"],'
            '"result_columns":["结果列"],"sql_constraints":[],'
            '"result_checks":[],"claim_extractions":[{"mode":"scalar",'
            '"name":"事实","field":"结果列","required":true}]'
            '}]}}],"consumes_pending":false,"execution_constraints":{"forbidden_tools":[],'
            '"required_artifacts":[],"forbidden_artifact_kinds":[]}}}。'
            "series extraction 必须带 aggregate 或 group_by，禁止用未聚合明细充当关系或分组证据。"
            "若当前 Session 有待澄清事项且这次问题明确补齐它，才把 consumes_pending 设为 true，"
            "否则保持 false；不能消费不存在的待办。"
            "保持首版目标的数量和粒度，不要凭空扩展成多个独立分析；"
            "只有用户明确要求的产物才加入 required_artifacts。"
            "如果原问题明确要求 Markdown、表格、图表或报告，且目标是当前数据的数值或分组，"
            "必须在最终 ready 计划中保留对应"
            "required_artifacts；不能因为 Discovery 观察不完整就返回 clarification，也不能把一次"
            "探索观察当成任务完成。此时应提交能继续执行的最小 evidence assertion，缺少的细节"
            "留给正式 SQL 和结果校验，不要丢失用户的交付要求。"
            "结构或关系目标应定稿为 context_only，不要写成未聚合明细 series。"
            "Discovery 观察中的列名、行数和引用只用于定义下一步可验证目标，不是 Evidence、Claim"
            "或最终数据事实；若仍无法定义目标，应返回 clarification，不得把观察值写进答案。"
            "如果首版是 discovery，最终计划只能使用 required_schema 中可见的表和字段；"
            "不能因为原始数据源可能存在其它字段而扩大范围。"
            "每个 requirement 最多 "
            f"{self._limits.agent_max_assertions_per_requirement} 个 assertions，"
            f"单个 Run 最多 {self._limits.agent_max_total_assertions} 个 assertions；"
            "宽泛探索只选择 3～4 个最能回答原问题的核心检查，低优先级质量检查不能全部变成"
            "强制 Evidence。"
            "DataLink 不可用时只能依据 Schema 形成可验证计划、"
            "提出真实澄清，或将受阻目标标记为 blocked。\n"
            ""
        )
        finalization_payload = f"定稿请求（不含重复的 Session 历史）：{request_json}"
        repair_attempted = False
        pending_issues: Sequence[OpeningValidationIssue] = ()
        while True:
            finalization_messages = [
                SystemMessage(content=prompt),
                HumanMessage(
                    content=_finalization_human_content(finalization_payload, pending_issues)
                ),
            ]
            if not self._request_fits_budget(finalization_messages, [finalize_analysis_plan]):
                return AgentFailure(
                    code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                    message="分析计划定稿请求超过模型上下文预算",
                )
            timeout_seconds = (
                self._deadline.discovery_plan_followup_remaining_seconds()
                if request.initial_plan.mode == "discovery"
                else self._deadline.preparation_stage_remaining_seconds("analysis_plan_followup")
            )
            try:
                response = await self._ainvoke(
                    self._model.bind_tools(
                        [finalize_analysis_plan], strict=True, parallel_tool_calls=False
                    ),
                    finalization_messages,
                    cancellation,
                    timeout_seconds=timeout_seconds,
                )
            except RuntimeWaitCanceled:
                return _cancelled_failure()
            except RuntimeWaitTimedOut:
                return AgentFailure(
                    code=AgentErrorCode.ANALYSIS_PREPARATION_BUDGET_EXHAUSTED,
                    message="分析计划定稿超过阶段时限",
                )
            except Exception as exc:
                if _is_context_length_error(exc):
                    return AgentFailure(
                        code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                        message="分析计划定稿请求超过供应商上下文限制",
                    )
                return _request_failure("分析计划定稿模型请求失败")
            call, issues = _inspect_finalization_response(response)
            if call is not None:
                try:
                    normalized_arguments = _normalize_plan_arguments(call.get("args"))
                    arguments = AnalysisPlanFinalizationArguments.model_validate(
                        normalized_arguments, strict=True
                    )
                except _PlanArgumentsNormalizationError as exc:
                    issues = [_plan_normalization_issue(exc)]
                except ValidationError as exc:
                    issues = _opening_validation_issues(exc, input_value=normalized_arguments)
                else:
                    return arguments.plan
            if not issues:
                return _invalid_plan_failure()
            if repair_attempted:
                return _invalid_plan_failure(issues)
            repair_attempted = True
            pending_issues = issues

    async def generate_final_answer(
        self,
        messages: Sequence[BaseMessage],
        mode: FinalOutputMode,
        cancellation: CancellationSignal,
    ) -> FinalMarkdownPayload | AgentFailure:
        """在独立请求中生成统一的最终答案 DTO。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        if not self._request_fits_budget(messages, self._final_answer_tools(mode)):
            return AgentFailure(
                code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                message="最终答案请求超过模型上下文预算",
            )
        try:
            if mode == "markdown":
                response = await self._ainvoke(
                    self._model,
                    messages,
                    cancellation,
                    timeout_seconds=self._deadline.final_answer_remaining_seconds(),
                )
                content = getattr(response, "content", None)
                if not isinstance(content, str) or not content.strip():
                    return _invalid_output_failure()
                return FinalMarkdownPayload(markdown=content.strip())
            if mode == "json_schema":
                response = await self._ainvoke(
                    self._model.with_structured_output(
                        FinalMarkdownPayload,
                        method="json_schema",
                        strict=True,
                    ),
                    messages,
                    cancellation,
                    timeout_seconds=self._deadline.final_answer_remaining_seconds(),
                )
                return FinalMarkdownPayload.model_validate(response, strict=True)
            if mode == "json_object":
                response = await self._ainvoke(
                    self._model.bind(response_format={"type": "json_object"}),
                    messages,
                    cancellation,
                    timeout_seconds=self._deadline.final_answer_remaining_seconds(),
                )
                content = getattr(response, "content", None)
                if not isinstance(content, str):
                    return _invalid_output_failure()
                return FinalMarkdownPayload.model_validate_json(content, strict=True)
            response = await self._ainvoke(
                self._model.bind_tools([submit_answer], strict=True, parallel_tool_calls=False),
                messages,
                cancellation,
                timeout_seconds=self._deadline.final_answer_remaining_seconds(),
            )
            return _parse_submit_answer(response)
        except RuntimeWaitCanceled:
            return _cancelled_failure()
        except RuntimeWaitTimedOut:
            return (
                _final_answer_timeout_failure()
                if self._deadline.remaining_seconds() > 0
                else _analysis_limit_failure()
            )
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return _invalid_output_failure()
        except Exception as exc:
            if _is_context_length_error(exc):
                return _context_budget_failure()
            logger.warning(
                "Final answer model request failed",
                extra={
                    "error_code": AgentErrorCode.MODEL_REQUEST_FAILED.value,
                    "error_type": type(exc).__name__,
                },
            )
            return _request_failure("模型请求失败")

    async def generate_final_answer_stream(
        self,
        messages: Sequence[BaseMessage],
        cancellation: CancellationSignal,
    ) -> AsyncIterator[str | AgentFailure] | AgentFailure:
        """以文本分片生成 Markdown；其他最终答案协议仍使用一次性调用。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        if self._deadline.final_remaining_seconds() <= 0:
            return _analysis_limit_failure()
        if not self._request_fits_budget(messages, ()):
            return _context_budget_failure()

        async def chunks() -> AsyncIterator[str | AgentFailure]:
            stream: Any | None = None
            emitted = False
            try:
                try:
                    stream = self._model.astream(list(messages))
                    iterator = stream.__aiter__()
                except Exception as exc:
                    logger.warning(
                        "Final answer streaming setup failed",
                        extra={
                            "error_code": AgentErrorCode.MODEL_REQUEST_FAILED.value,
                            "error_type": type(exc).__name__,
                        },
                    )
                    yield _request_failure("模型请求失败")
                    return
                while True:
                    timeout_seconds = (
                        self._deadline.final_answer_idle_remaining_seconds()
                        if emitted
                        else self._deadline.final_answer_first_token_remaining_seconds()
                    )
                    try:
                        chunk = await await_runtime_call(
                            iterator.__anext__(),
                            cancellation=cancellation,
                            timeout_seconds=timeout_seconds,
                        )
                    except StopAsyncIteration:
                        return
                    except RuntimeWaitCanceled:
                        yield _cancelled_failure()
                        return
                    except RuntimeWaitTimedOut:
                        yield (
                            _final_answer_timeout_failure()
                            if self._deadline.remaining_seconds() > 0
                            else _analysis_limit_failure()
                        )
                        return
                    except Exception as exc:
                        if _is_context_length_error(exc):
                            yield _context_budget_failure()
                            return
                        logger.warning(
                            "Final answer streaming request failed",
                            extra={
                                "error_code": AgentErrorCode.MODEL_REQUEST_FAILED.value,
                                "error_type": type(exc).__name__,
                            },
                        )
                        yield _request_failure("模型请求失败")
                        return
                    text = _stream_chunk_text(chunk)
                    if text:
                        emitted = True
                        yield text
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    try:
                        await asyncio.wait_for(close(), timeout=0.1)
                    except TimeoutError:
                        logger.warning(
                            "Final answer stream close exceeded cleanup budget",
                            extra={"error_code": AgentErrorCode.FINAL_ANSWER_TIMEOUT.value},
                        )
                    except Exception:
                        logger.debug("Final answer stream close failed", exc_info=True)

        return chunks()

    def _final_answer_tools(self, mode: FinalOutputMode) -> Sequence[BaseTool]:
        if mode == "submit_answer":
            return [submit_answer]
        return ()

    def _request_fits_budget(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
    ) -> bool:
        """Deterministically reject oversized requests before provider I/O."""

        estimate = self._estimate_request_budget(messages, tools)
        self.last_request_budget = estimate
        return estimate.estimated_total_tokens <= estimate.context_window_tokens

    def _estimate_request_budget(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
    ) -> RequestBudgetEstimate:
        """计算模型输入预算，只返回可安全观测的数量和来源。"""

        context_window = (
            self._model_context.context_window_tokens
            if self._model_context
            else self._limits.context_window_tokens
        )
        output_budget = self._model_context.output_budget_tokens if self._model_context else 4_096
        safety_margin = self._model_context.safety_margin_tokens if self._model_context else 1_024
        system_budget = self._model_context.system_budget_tokens if self._model_context else 4_096
        reserved_tool_tokens = (
            self._model_context.tool_schema_cost if self._model_context else 8_192
        )
        actual_tool_tokens = _estimate_tool_schema_tokens(tools)
        message_tokens = _estimate_message_tokens(messages)
        tool_tokens = max(reserved_tool_tokens, actual_tool_tokens) if tools else 0
        estimated_total_tokens = (
            message_tokens + system_budget + output_budget + tool_tokens + safety_margin
        )
        return RequestBudgetEstimate(
            input_chars=sum(_message_input_chars(message) for message in messages),
            input_tokens=message_tokens,
            estimated_total_tokens=estimated_total_tokens,
            context_window_tokens=context_window,
            input_budget_tokens=max(
                0, context_window - output_budget - system_budget - tool_tokens - safety_margin
            ),
            remaining_tokens=max(0, context_window - estimated_total_tokens),
            # The adapter has no provider tokenizer, so all local estimates use
            # the same conservative UTF-8 approximation.  Context-window origin
            # is recorded separately as ``context_window_source`` on the Run.
            estimate_source="utf8_estimate",
        )

    async def _ainvoke(
        self,
        runnable: Any,
        messages: Sequence[BaseMessage],
        cancellation: CancellationSignal,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        remaining = self._deadline.remaining_seconds()
        if timeout_seconds is not None:
            remaining = min(remaining, timeout_seconds)
        if remaining <= 0:
            raise RuntimeWaitTimedOut
        return await await_runtime_call(
            runnable.ainvoke(list(messages)),
            cancellation=cancellation,
            timeout_seconds=remaining,
        )


def _parse_submit_answer(response: object) -> FinalMarkdownPayload:
    if not isinstance(response, AIMessage):
        raise ValueError("submit_answer response is not an AI message")
    tool_calls = response.tool_calls
    if not isinstance(tool_calls, list):
        raise ValueError("submit_answer tool call is missing")
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or tool_call.get("name") != _SUBMIT_ANSWER_TOOL_NAME:
            continue
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        return FinalMarkdownPayload.model_validate(tool_call.get("args"), strict=True)
    raise ValueError("submit_answer tool call is invalid")


def _context_budget_failure() -> AgentFailure:
    return AgentFailure(
        code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
        message="最终答案请求超过模型上下文预算",
    )


def _estimate_message_tokens(messages: Sequence[BaseMessage]) -> int:
    """Use a conservative deterministic estimate when no provider tokenizer exists."""

    return estimate_message_tokens(messages)


def _message_input_chars(message: BaseMessage) -> int:
    """计算消息及原生工具调用的安全字符数，不返回消息内容。"""

    chars = len(str(message.content))
    if isinstance(message, AIMessage) and message.tool_calls:
        chars += len(
            json.dumps(
                message.tool_calls,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        )
    return chars


def _estimate_tool_schema_tokens(tools: Sequence[BaseTool]) -> int:
    """Estimate the serialized tool contract, including names and descriptions."""

    if not tools:
        return 0
    payload: list[dict[str, object]] = []
    for item in tools:
        schema = item.args_schema
        if hasattr(schema, "model_json_schema"):
            schema_value = schema.model_json_schema()
        else:
            schema_value = {}
        payload.append(
            {
                "name": item.name,
                "description": item.description or "",
                "parameters": schema_value,
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    return max(1, (len(encoded) + 3) // 4)


def _stream_chunk_text(chunk: object) -> str:
    """提取 AIMessageChunk 的文本，忽略工具/多模态等非文本块。"""

    direct_text = getattr(chunk, "text", None)
    if isinstance(direct_text, str):
        return direct_text
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _is_analysis_commit_only(tools: Sequence[BaseTool]) -> bool:
    """只在 Graph 收敛到内部结论提交工具时启用短模型窗口。"""

    return bool(tools) and all(tool_item.name == _ANALYSIS_COMMIT_TOOL_NAME for tool_item in tools)


def _is_context_length_error(error: BaseException) -> bool:
    """只把供应商明确的上下文超限归类为可压缩错误。"""

    text = str(error).casefold()
    markers = (
        "context length",
        "context_length",
        "maximum context",
        "max context",
        "prompt is too long",
        "too many tokens",
        "上下文超过",
    )
    return any(marker in text for marker in markers)


def _cancelled_failure() -> AgentFailure:
    return AgentFailure(code=AgentErrorCode.RUN_CANCELED, message="分析已取消")


def _request_failure(message: str) -> AgentFailure:
    return AgentFailure(code=AgentErrorCode.MODEL_REQUEST_FAILED, message=message)


def _analysis_limit_failure() -> AgentFailure:
    return AgentFailure(
        code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
        message="本次分析已达到全局时限",
    )


class _PlanArgumentsNormalizationError(ValueError):
    """模型 ToolCall 的 plan 包装不符合唯一允许的归一化形状。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize_plan_arguments(input_value: object) -> dict[str, object]:
    """只把 args.plan 的单层 JSON 对象字符串解码一次。"""

    if not isinstance(input_value, Mapping):
        raise _PlanArgumentsNormalizationError("args_not_object")
    normalized = dict(input_value)
    raw_plan = normalized.get("plan")
    if isinstance(raw_plan, Mapping):
        normalized["plan"] = dict(raw_plan)
        return normalized
    if not isinstance(raw_plan, str):
        return normalized
    if not raw_plan or len(raw_plan) > _MAX_SINGLE_ENCODED_PLAN_CHARS:
        raise _PlanArgumentsNormalizationError(
            "plan_too_long" if len(raw_plan) > _MAX_SINGLE_ENCODED_PLAN_CHARS else "plan_empty"
        )
    try:
        decoded = json.loads(raw_plan)
    except json.JSONDecodeError as exc:
        raise _PlanArgumentsNormalizationError("plan_json_invalid") from exc
    if not isinstance(decoded, Mapping):
        raise _PlanArgumentsNormalizationError("decoded_plan_not_object")
    normalized["plan"] = dict(decoded)
    return normalized


def _plan_normalization_issue(
    error: _PlanArgumentsNormalizationError,
) -> OpeningValidationIssue:
    details = {
        "args_not_object": (
            "args",
            "dict_type",
            "ToolCall args 类型不符合合同",
            "args 必须是对象",
            "保持 args 为对象，并在 plan 字段提交完整计划。",
        ),
        "plan_empty": (
            "plan",
            "json_invalid",
            "plan 字符串为空",
            "plan 必须是非空 JSON 对象或对象",
            "重新提交完整 plan 对象。",
        ),
        "plan_too_long": (
            "plan",
            "string_too_long",
            "plan 字符串超过长度上限",
            f"plan 字符串最多 {_MAX_SINGLE_ENCODED_PLAN_CHARS} 个字符",
            "缩短计划并重新提交完整 plan 对象。",
        ),
        "plan_json_invalid": (
            "plan",
            "json_invalid",
            "plan 不是单个完整 JSON 值",
            "plan 必须是 JSON 对象或对象",
            "删除代码围栏、尾随文本或损坏的 JSON，并重新提交完整 plan。",
        ),
        "decoded_plan_not_object": (
            "plan",
            "dict_type",
            "plan 单次解码后不是对象",
            "plan 单次解码后必须是对象",
            "不要提交数组、标量或二次编码字符串；重新提交完整 plan 对象。",
        ),
    }
    path, error_type, actual, expected, action = details[error.reason]
    return OpeningValidationIssue(
        path=path,
        error_type=error_type,
        repair_reason="shape_invalid",
        actual=actual,
        expected=expected,
        rule="ToolCall args 必须是对象；只允许 args.plan 被单层编码为 JSON 对象字符串",
        action=action,
    )


def _opening_attempt_summary(input_value: object) -> RunOpeningAttemptSummary:
    """从唯一分析 action 提取枚举与计数，丢弃全部业务文本和值。"""

    raw_plan = input_value.get("plan") if isinstance(input_value, Mapping) else None
    if isinstance(raw_plan, str) and 0 < len(raw_plan) <= _MAX_SINGLE_ENCODED_PLAN_CHARS:
        try:
            decoded = json.loads(raw_plan)
        except json.JSONDecodeError:
            decoded = None
        raw_plan = decoded
    if not isinstance(raw_plan, Mapping):
        return RunOpeningAttemptSummary(attempted_protocol="data-analysis")

    raw_mode = raw_plan.get("mode")
    trusted_mode = (
        raw_mode
        if raw_mode in {"ready", "discovery", "needs_semantic_context", "clarification"}
        else None
    )
    requirements = raw_plan.get("requirements")
    safe_requirements = requirements[:64] if isinstance(requirements, list) else []
    fulfillment_count = 0
    assertion_count = 0
    for item in safe_requirements:
        fulfillment = item.get("fulfillment") if isinstance(item, Mapping) else None
        if not isinstance(fulfillment, Mapping):
            continue
        fulfillment_count += 1
        assertions = fulfillment.get("assertions")
        if isinstance(assertions, list):
            assertion_count = min(256, assertion_count + len(assertions))
    constraints = raw_plan.get("execution_constraints")
    artifacts = constraints.get("required_artifacts") if isinstance(constraints, Mapping) else None
    return RunOpeningAttemptSummary(
        attempted_protocol="data-analysis",
        trusted_mode=trusted_mode,
        requirement_count=len(safe_requirements),
        fulfillment_count=fulfillment_count,
        assertion_count=assertion_count,
        artifact_count=min(64, len(artifacts)) if isinstance(artifacts, list) else 0,
    )


def _opening_invalid_failure(
    validation_issues: Sequence[OpeningValidationIssue] = (),
    *,
    attempt_summary: RunOpeningAttemptSummary | None = None,
) -> RunOpeningInvalidFailure:
    return RunOpeningInvalidFailure(
        message="模型 Opening 没有返回合法的普通文本或分析 action",
        retryable=True,
        validation_issues=list(validation_issues),
        attempt_summary=attempt_summary,
    )


def _opening_validation_issues(
    error: ValidationError, *, input_value: object = None
) -> list[OpeningValidationIssue]:
    """把 Pydantic 错误压缩为不含原始值的 repair 指令。"""

    pending: list[dict[str, object]] = []
    safe_plan = _safe_opening_plan_projection(input_value)
    for item in error.errors(include_input=False, include_url=False):
        error_type = str(item.get("type") or "validation_error")[:80]
        path = _opening_error_path(item.get("loc"))
        context = item.get("ctx")
        value_error_projection = _opening_value_error_projection(
            error_type,
            path,
            context,
            safe_plan,
        )
        if value_error_projection is not None:
            path, repair_reason, actual, expected, rule, action, suggested_mode = (
                value_error_projection
            )
        else:
            action = _opening_error_action(error_type, path, context)
            repair_reason = _opening_repair_reason(error_type, path, context)
            actual, expected, rule = _opening_error_projection(
                error_type,
                path,
                context,
                input_value,
            )
            suggested_mode = None
        pending.append(
            {
                "path": path,
                "error_type": error_type,
                "repair_reason": repair_reason,
                "actual": actual,
                "expected": expected,
                "rule": rule,
                "action": action,
                "suggested_mode": suggested_mode,
            }
        )

    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for item in pending:
        key = (
            item["path"],
            item["error_type"],
            item["repair_reason"],
            item["action"],
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {**item, "occurrences": 1}
        else:
            existing["occurrences"] = min(64, int(existing["occurrences"]) + 1)
    ordered = sorted(
        grouped.values(),
        key=lambda item: (
            str(item["path"]),
            str(item["error_type"]),
            str(item["repair_reason"]),
            str(item["action"]),
        ),
    )
    return [OpeningValidationIssue(**item) for item in ordered[:12]]


def _safe_opening_plan_projection(value: object) -> dict[str, object]:
    """只提取 root 计划合同诊断所需的安全枚举和数量。"""

    if not isinstance(value, Mapping):
        return {}
    raw_plan = value.get("plan")
    if not isinstance(raw_plan, Mapping):
        return {}
    raw_mode = raw_plan.get("mode")
    raw_requirements = raw_plan.get("requirements")
    requirements: list[dict[str, object]] = []
    if isinstance(raw_requirements, list):
        for item in raw_requirements[:64]:
            fulfillment = item.get("fulfillment") if isinstance(item, Mapping) else None
            mode = fulfillment.get("mode") if isinstance(fulfillment, Mapping) else None
            requirements.append({"fulfillment": {"mode": mode}} if isinstance(mode, str) else {})
    raw_constraints = raw_plan.get("execution_constraints")
    required_artifacts: list[object] = []
    if isinstance(raw_constraints, Mapping) and isinstance(
        raw_constraints.get("required_artifacts"), list
    ):
        required_artifacts = [{} for _ in raw_constraints["required_artifacts"][:64]]
    return {
        "mode": (
            raw_mode
            if isinstance(raw_mode, str)
            and raw_mode in {"ready", "discovery", "needs_semantic_context", "clarification"}
            else None
        ),
        "requirements": requirements,
        "execution_constraints": {"required_artifacts": required_artifacts},
    }


def _opening_repair_reason(error_type: str, path: str, context: object) -> str:
    if error_type == "value_error" and isinstance(context, dict):
        detail = str(context.get("error") or "")
        if "discovery plan cannot contain evidence assertions" in detail:
            return "protocol_direction_conflict"
    if error_type in {"missing", "extra_forbidden", "union_tag_not_found", "literal_error"}:
        return "shape_invalid"
    if "clarification" in path or "requirements" in path:
        return "scope_incomplete"
    return "shape_invalid"


def _opening_error_projection(
    error_type: str,
    path: str,
    context: object,
    input_value: object = None,
) -> tuple[str, str, str]:
    """将 Pydantic 错误转换为安全的实际值、期望值和规则说明。"""

    leaf = path.rsplit(".", 1)[-1] if path else "参数"
    if error_type == "missing":
        return "缺失", f"必须提供 {leaf}", "Tool Schema 标记为 required 的字段必须存在"
    if error_type == "extra_forbidden":
        return (
            _safe_model_scalar(leaf, fallback="额外字段"),
            "只允许 Tool Schema 声明的字段",
            "Tool Schema 禁止额外字段",
        )
    if error_type == "union_tag_not_found":
        return (
            "缺少联合类型标记",
            "必须显式填写对应的 mode 或 kind",
            "联合类型必须通过 discriminator 选择具体形状",
        )
    if error_type == "literal_error":
        expected = "Tool Schema 允许的枚举值"
        if isinstance(context, dict) and context.get("expected"):
            expected = str(context["expected"])[:240]
        return (
            _safe_model_scalar(input_value, fallback="枚举值不合法"),
            expected,
            "字段只能使用合同声明的枚举值",
        )
    if error_type.endswith("_type") or error_type in {"model_type", "list_type", "dict_type"}:
        return "类型不符合合同", "符合 Tool Schema 声明的类型", "字段类型必须符合结构化 Tool Schema"
    if error_type == "value_error":
        return "计划形状冲突", "符合当前 mode 的字段组合", "计划的字段组合必须满足 mode 合同"
    return "未通过结构化校验", "符合 Tool Schema", "Opening 参数必须通过结构化校验"


def _opening_value_error_projection(
    error_type: str,
    path: str,
    context: object,
    input_value: object,
) -> tuple[str, str, str, str, str, str, str | None] | None:
    """把根级 mode 合同错误定位到具体字段，并只投影安全计数/枚举。"""

    if error_type != "value_error" or not isinstance(context, dict):
        return None
    detail = str(context.get("error") or "")
    plan = input_value if isinstance(input_value, dict) else {}

    def count_items(name: str) -> int:
        value = plan.get(name)
        return len(value) if isinstance(value, list) else 0

    def required_artifact_count() -> int:
        constraints = plan.get("execution_constraints")
        if not isinstance(constraints, dict):
            return 0
        value = constraints.get("required_artifacts")
        return len(value) if isinstance(value, list) else 0

    def path_for(name: str) -> str:
        return f"{path}.{name}" if path else name

    if "analysis plan requires at least one goal" in detail:
        return (
            path_for("requirements"),
            "scope_incomplete",
            f"{count_items('requirements')} 项",
            "至少 1 个可执行目标",
            "ready、discovery 和 needs_semantic_context 必须包含 requirements",
            "补充具体目标；如果用户口径不足，完整提交 clarification 计划。",
            "clarification",
        )
    if "clarification plan requires only a clarification" in detail:
        return (
            path_for("requirements"),
            "shape_invalid",
            f"{count_items('requirements')} 项",
            "clarification 计划的 requirements 必须为空",
            "clarification 只能包含一条结构化 clarification",
            "删除 requirements，只保留 clarification.question 和 missing_items。",
            "clarification",
        )
    if "clarification plan cannot request semantic context" in detail:
        return (
            path_for("semantic_request"),
            "shape_invalid",
            "已提供",
            "该字段必须缺失",
            "clarification 不能同时请求 semantic_context",
            "删除 plan.semantic_request 后重新提交完整 clarification plan。",
            "clarification",
        )
    if "clarification plan cannot include discovery scope" in detail:
        return (
            path_for("discovery_scope"),
            "shape_invalid",
            "已提供",
            "该字段必须缺失",
            "clarification 不能同时携带 Discovery scope",
            "删除 plan.discovery_scope 后重新提交完整 clarification plan。",
            "clarification",
        )
    if "clarification plan cannot require artifacts" in detail:
        return (
            path_for("execution_constraints.required_artifacts"),
            "artifact_conflict",
            f"{required_artifact_count()} 项",
            "clarification 不得要求正式 Artifact",
            "没有 Evidence 的 clarification 不能创建正式产物",
            "删除 required_artifacts，或改为完整 evidence 分析计划。",
            "clarification",
        )
    if "non-clarification plan cannot contain clarification" in detail:
        return (
            path_for("clarification"),
            "shape_invalid",
            "已提供",
            "只有 mode=clarification 才能填写该字段",
            "非 clarification 计划不能携带 clarification",
            "删除 plan.clarification，或把 mode 改成 clarification 并删除 requirements。",
            None,
        )
    if "semantic request is only valid for needs_semantic_context" in detail:
        current_mode = plan.get("mode")
        suggested_mode = (
            current_mode
            if isinstance(current_mode, str) and current_mode in {"ready", "discovery"}
            else None
        )
        return (
            path_for("semantic_request"),
            "shape_invalid",
            "已提供",
            "仅 mode=needs_semantic_context 可填写",
            "semantic_request 只能出现在 needs_semantic_context 计划",
            "删除 semantic_request，保持原 mode。",
            suggested_mode,
        )
    if "discovery scope is only valid for discovery mode" in detail:
        return (
            path_for("discovery_scope"),
            "shape_invalid",
            "已提供",
            "仅 mode=discovery 可填写",
            "discovery_scope 只能出现在 discovery 计划",
            "删除 discovery_scope，或把 mode 改为 discovery 并补齐受限探索目标。",
            "discovery",
        )
    if "semantic context plan requires a semantic request" in detail:
        return (
            path_for("semantic_request"),
            "scope_incomplete",
            "缺失",
            "mode=needs_semantic_context 时必须提供 semantic_request",
            "语义定稿前必须有一次受限 semantic_request",
            "补充 semantic_request.query，并重新提交完整计划。",
            None,
        )
    if "discovery plan requires a discovery scope" in detail:
        return (
            path_for("discovery_scope"),
            "discovery_scope_missing",
            "缺失",
            "mode=discovery 时必须提供 tables、columns 和 max_rows",
            "Discovery 只能在 Schema 校验过的有限 scope 内探索",
            "补充 discovery_scope 的 tables、columns 和 max_rows；不要改成 ready 伪造最终指标。",
            "discovery",
        )
    if "discovery plan cannot contain evidence assertions" in detail:
        requirements = plan.get("requirements")
        evidence_index = (
            next(
                (
                    index
                    for index, item in enumerate(requirements)
                    if isinstance(item, dict)
                    and isinstance(item.get("fulfillment"), dict)
                    and item["fulfillment"].get("mode") == "evidence"
                ),
                0,
            )
            if isinstance(requirements, list)
            else 0
        )
        return (
            f"{path}.requirements[{evidence_index}].fulfillment.mode"
            if path
            else f"requirements[{evidence_index}].fulfillment.mode",
            "protocol_direction_conflict",
            "evidence",
            "discovery 只能包含 context_only 或 blocked 目标",
            "Discovery 观察不是正式 Evidence/Claim 计划",
            (
                "如果已经明确最终指标和证据，请把 plan.mode 改为 ready；否则删除 "
                "evidence assertion 并保留受限 discovery。"
            ),
            "ready",
        )
    if "discovery plan cannot require artifacts" in detail:
        return (
            path_for("execution_constraints.required_artifacts"),
            "artifact_conflict",
            "已提供",
            "discovery 不得要求正式 Artifact",
            "Discovery 观察不能直接生成正式交付物",
            "删除 required_artifacts；完成 Discovery 后再由 ready evidence 计划声明产物。",
            "discovery",
        )
    return None


def _safe_model_scalar(value: object, *, fallback: str) -> str:
    """只回显短、无控制字符的标量；复杂对象只显示类型，避免泄露原始输入。"""

    if isinstance(value, str):
        normalized = " ".join(value.split()).strip()
        if (
            normalized
            and len(normalized) <= 160
            and all(ord(char) >= 32 for char in normalized)
            and _safe_opening_scalar_text(normalized)
        ):
            return normalized
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, int | float):
        return f"数值 {value}"
    return fallback


def _safe_opening_scalar_text(value: str) -> bool:
    """拒绝可能携带凭据、路径、URL 或 SQL 的错误输入原文。"""

    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "password",
            "passwd",
            "api_key",
            "apikey",
            "secret",
            "token",
            "authorization",
            "credential",
            "bearer ",
        )
    ):
        return False
    if value.startswith(("/", "\\")) or re.match(r"^[a-z]:[\\/]", value, re.IGNORECASE):
        return False
    if "://" in value or "\\\\" in value:
        return False
    if ";" in value:
        return False
    sql_markers = (
        "select ",
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        " from ",
        " where ",
    )
    return not any(marker in lowered for marker in sql_markers)


def _opening_error_path(value: object) -> str:
    if not isinstance(value, tuple | list):
        return "args"
    result = ""
    for part in value:
        if isinstance(part, int):
            result += f"[{part}]"
        elif isinstance(part, str) and part:
            result += ("." if result else "") + _safe_opening_path_part(part)
    return result or "args"


def _safe_opening_path_part(value: str) -> str:
    """保留可定位的字段名，替换可能携带密钥、路径或 SQL 的键名。"""

    text = " ".join(value.split()).strip()
    if not text or len(text) > 120 or any(ord(char) < 32 for char in text):
        return "额外字段"
    lowered = text.casefold()
    if "=" in text or "/" in text or "\\" in text or "://" in text or ";" in text:
        return "额外字段"
    if any(marker in lowered for marker in ("select ", "insert ", "update ", "delete ", "drop ")):
        return "额外字段"
    return text


def _opening_error_action(error_type: str, path: str, context: object) -> str:
    if error_type == "union_tag_not_found":
        if "claim_extractions" in path:
            return (
                "在每个 claim_extractions 对象内显式添加 mode；标量为 scalar，分组序列为 series。"
            )
        if "fulfillment" in path:
            return "在 fulfillment 对象内显式添加 mode：evidence、context_only 或 blocked。"
        if "sql_constraints" in path:
            return "在每个 sql_constraints 对象内显式添加对应的 kind。"
        if "result_checks" in path:
            return "在每个 result_checks 对象内显式添加对应的 kind。"
        discriminator = "mode 或 kind"
        if isinstance(context, dict):
            raw_discriminator = context.get("discriminator")
            if isinstance(raw_discriminator, str):
                discriminator = raw_discriminator.strip("'")[:40]
        return f"在该联合类型对象内显式添加 discriminator {discriminator}。"
    if error_type == "extra_forbidden":
        if path == "semantic_request":
            return (
                "删除 start_data_analysis 顶层 semantic_request；仅当 plan.mode="
                "needs_semantic_context 时放入 plan.semantic_request。"
            )
        if ".fulfillment." in path and path.rsplit(".", 1)[-1] in {
            "result_columns",
            "source_tables",
            "sql_constraints",
            "claim_extractions",
        }:
            return "从 fulfillment 层删除该字段，并放入对应的 fulfillment.assertions[*] 对象。"
        return "删除该对象中 Tool Schema 未声明的字段。"
    if error_type == "missing":
        if path.endswith(".fulfillment"):
            return (
                "补充该 requirement 的 fulfillment；Discovery 目标使用 mode=context_only、"
                "sources=[schema]，ready 目标使用 mode=evidence 并提供 assertions。"
            )
        return "补充 Tool Schema 标记为 required 的字段。"
    if error_type == "literal_error":
        return "把该字段改为 Tool Schema 允许的枚举值。"
    if error_type == "value_error" and isinstance(context, dict):
        detail = str(context.get("error") or "")
        if "discovery plan cannot contain evidence assertions" in detail:
            return (
                "该计划已经包含可执行 evidence assertions；把 plan.mode 改为 ready。"
                "只有尚不能定义正式证据目标时才使用 discovery。"
            )
    return "按 Tool Schema 修正该路径的类型、层级或取值，不要添加未声明字段。"


def _invalid_plan_failure(
    validation_issues: Sequence[OpeningValidationIssue] = (),
) -> AgentFailure:
    return AnalysisPlanInvalidFailure(
        code=AgentErrorCode.ANALYSIS_PLAN_INVALID,
        message="模型没有返回合法的最终分析计划",
        validation_issues=list(validation_issues)[:12],
    )


def _finalization_tool_call_counts(response: AIMessage) -> tuple[int, int]:
    valid = response.tool_calls if isinstance(response.tool_calls, list) else []
    invalid = getattr(response, "invalid_tool_calls", None)
    invalid_calls = invalid if isinstance(invalid, list) else []
    return len(valid), len(invalid_calls)


def _finalization_count_actual(valid_count: int, invalid_count: int, extra: str = "") -> str:
    summary = f"valid={valid_count} invalid={invalid_count}"
    if extra:
        summary = f"{summary} {extra}"
    return summary[:240]


def _invalid_tool_call_kind(response: AIMessage) -> str:
    raw = getattr(response, "invalid_tool_calls", None)
    if not isinstance(raw, list):
        return "invalid_tool_call"
    for item in raw:
        error = item.get("error") if isinstance(item, Mapping) else getattr(item, "error", None)
        if not isinstance(error, str):
            continue
        if "JSONDecodeError" in error or "json" in error.lower():
            return "json_decode_error"
    return "invalid_tool_call"


def _inspect_finalization_response(
    response: object,
) -> tuple[dict[str, object] | None, list[OpeningValidationIssue]]:
    """分类定稿响应形状；不读取或回传非法 args / 解析错误原文。"""

    if not isinstance(response, AIMessage):
        return None, [
            _finalization_issue(
                "response",
                "response_shape_invalid",
                "返回 AI 消息",
                actual=_finalization_count_actual(0, 0),
            )
        ]
    valid_count, invalid_count = _finalization_tool_call_counts(response)
    count_actual = _finalization_count_actual(valid_count, invalid_count)
    if _stream_chunk_text(response).strip():
        return None, [
            _finalization_issue(
                "response.content",
                "unexpected_text",
                "删除正文，只提交唯一定稿工具调用",
                actual=count_actual,
            )
        ]
    if invalid_count:
        return None, [
            _finalization_issue(
                "response.invalid_tool_calls",
                "json_invalid",
                "参数 JSON 不合法，请重新提交一次合法 JSON 对象的 finalize_analysis_plan 调用",
                actual=_finalization_count_actual(
                    valid_count, invalid_count, _invalid_tool_call_kind(response)
                ),
            )
        ]
    if valid_count != 1:
        return None, [
            _finalization_issue(
                "response.tool_calls",
                "count_invalid",
                "只提交一个 finalize_analysis_plan 调用",
                actual=count_actual,
            )
        ]
    call = response.tool_calls[0]
    if not isinstance(call, dict) or call.get("name") != "finalize_analysis_plan":
        return None, [
            _finalization_issue(
                "response.tool_calls[0].name",
                "tool_name_invalid",
                "使用 finalize_analysis_plan",
                actual=count_actual,
            )
        ]
    tool_call_id = call.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None, [
            _finalization_issue(
                "response.tool_calls[0].id",
                "tool_call_id_invalid",
                "提供有效工具调用标识",
                actual=count_actual,
            )
        ]
    return call, []


def _finalization_human_content(
    payload: str,
    issues: Sequence[OpeningValidationIssue],
) -> str:
    if not issues:
        return payload
    repair_json = json.dumps(
        [issue.model_dump(mode="json") for issue in issues],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{payload}\n本次脱敏校验 finding：{repair_json}\n"
        "请逐项修复 finding 指向的路径，并重新提交恰好一次无正文的 finalize_analysis_plan 调用；"
        "不要只返回局部补丁，不要输出解释正文。"
    )


def _finalization_issue(
    path: str,
    error_type: str,
    action: str,
    *,
    actual: str = "未提供",
) -> OpeningValidationIssue:
    """生成不包含模型正文或原始值的定稿形状诊断。"""

    return OpeningValidationIssue(
        path=path,
        error_type=error_type,
        repair_reason="shape_invalid",
        actual=actual,
        expected="唯一 finalize_analysis_plan 工具调用和合法 plan",
        rule="定稿响应必须是无正文的单一 finalize_analysis_plan ToolCall",
        action=action,
    )


def _analysis_claim_commit_timeout_failure() -> AgentFailure:
    """区分结论提交窗口耗尽与整轮 Run 总时限。"""

    return AgentFailure(
        code=AgentErrorCode.ANALYSIS_CLAIM_COMMIT_TIMEOUT,
        message="结论提交模型请求超过本阶段时限",
    )


def _analysis_agent_turn_timeout_failure() -> AgentFailure:
    """区分普通 Agent 单轮窗口耗尽与整轮 Run 总时限。"""

    return AgentFailure(
        code=AgentErrorCode.ANALYSIS_AGENT_TURN_TIMEOUT,
        message="Agent 分析回合超过本阶段时限",
    )


def _final_answer_timeout_failure() -> AgentFailure:
    """区分最终答案窗口耗尽与整轮 Run 总时限。"""

    return AgentFailure(
        code=AgentErrorCode.FINAL_ANSWER_TIMEOUT,
        message="最终答案模型请求超过本阶段时限",
    )


def _invalid_output_failure() -> AgentFailure:
    return AgentFailure(
        code=AgentErrorCode.MODEL_OUTPUT_INVALID, message="模型返回的最终答案格式无效"
    )
