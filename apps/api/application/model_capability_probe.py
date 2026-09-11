"""用真实 OpenAI-compatible 请求探测模型协议能力。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from contracts.model_profiles import FinalOutputMode
from pydantic import ValidationError

from application.model_runtime import build_openai_compatible_chat_model, start_data_analysis

_TOOL_PROBE_PROMPT = (
    "这是 DataPilot Opening 协议能力探测。只调用 start_data_analysis 一次，不要输出普通文本。"
    "start_data_analysis 顶层只能包含 plan。plan.mode=ready，requirements 只含一个 evidence 目标。"
    "目标包含 description、acceptance_criteria 和 fulfillment；fulfillment.mode=evidence，"
    "assertions 只含一个检查项。检查项必须包含 description、result_columns、sql_constraints、"
    "result_checks 和 claim_extractions：sql_constraints 使用显式 kind=source；"
    "result_checks 使用显式 kind=non_empty；claim_extractions 同时包含显式 mode=scalar 和"
    "mode=series。execution_constraints.required_artifacts 要求一个 kind=markdown 的产物，"
    "其余约束使用空数组。不要填写顶层 semantic_request，也不要依赖 mode 或 kind 默认值。"
)
_MARKDOWN_PROBE_PROMPT = (
    "这是协议能力探测。只返回 Markdown 原文，必须包含一个标题、一个空行和一段正文。"
    "不要返回 JSON，不要使用代码围栏，不要解释。"
)


@dataclass(frozen=True)
class ModelCapabilityProbeRequest:
    """单次探测所需的内存配置，调用结束后不持久化。"""

    model_name: str
    base_url: str
    api_key: str
    temperature: float
    timeout_seconds: int


@dataclass(frozen=True)
class ModelCapabilityProbeResult:
    """可安全回写到 Model Profile 的协议能力摘要。"""

    tool_calling_supported: bool
    final_output_mode: FinalOutputMode | None
    message: str


class ModelCapabilityProbe(Protocol):
    """Profile 服务的可替换探测边界，单元测试不调用真实模型。"""

    async def probe(self, request: ModelCapabilityProbeRequest) -> ModelCapabilityProbeResult: ...


ChatModelFactory = Callable[[ModelCapabilityProbeRequest], Any]


class LangChainModelCapabilityProbe:
    """按完整 Opening action、最终答案能力探测模型。"""

    def __init__(self, model_factory: ChatModelFactory | None = None) -> None:
        self._model_factory = model_factory or _build_chat_model

    async def probe(self, request: ModelCapabilityProbeRequest) -> ModelCapabilityProbeResult:
        model = self._model_factory(request)
        if not await self._supports_opening_contract(model):
            return ModelCapabilityProbeResult(
                tool_calling_supported=False,
                final_output_mode=None,
                message=(
                    "模型没有返回有效的 start_data_analysis Opening action，"
                    "请检查模型或兼容接口配置。"
                ),
            )

        if await self._supports_markdown(model):
            return ModelCapabilityProbeResult(
                tool_calling_supported=True,
                final_output_mode="markdown",
                message="Opening action 和直接 Markdown 输出已通过探测。",
            )
        return ModelCapabilityProbeResult(
            tool_calling_supported=True,
            final_output_mode="submit_answer",
            message="Opening action 已通过；最终答案将使用 submit_answer 兜底。",
        )

    async def _supports_opening_contract(self, model: Any) -> bool:
        try:
            response = await model.bind_tools(
                [start_data_analysis], strict=True, parallel_tool_calls=False
            ).ainvoke(_TOOL_PROBE_PROMPT)
        except Exception:
            return False

        tool_calls = getattr(response, "tool_calls", None)
        if not isinstance(tool_calls, list):
            return False
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            if tool_call.get("name") != "start_data_analysis":
                continue
            tool_call_id = tool_call.get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            try:
                start_data_analysis.args_schema.model_validate(tool_call.get("args"), strict=True)
            except ValidationError:
                continue
            return True
        return False

    async def _supports_markdown(self, model: Any) -> bool:
        try:
            response = await model.ainvoke(_MARKDOWN_PROBE_PROMPT)
        except Exception:
            return False
        content = getattr(response, "content", None)
        return isinstance(content, str) and bool(content.strip()) and "\n" in content


def _build_chat_model(request: ModelCapabilityProbeRequest):
    return build_openai_compatible_chat_model(
        model_name=request.model_name,
        base_url=request.base_url,
        api_key=request.api_key,
        temperature=request.temperature,
        timeout=request.timeout_seconds,
    )
