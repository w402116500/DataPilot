from __future__ import annotations

from dataclasses import dataclass

import pytest
from application.model_capability_probe import (
    _TOOL_PROBE_PROMPT,
    LangChainModelCapabilityProbe,
    ModelCapabilityProbeRequest,
)


@dataclass
class _ToolResponse:
    tool_calls: list[dict[str, object]]


@dataclass
class _TextResponse:
    content: object


class _Runnable:
    def __init__(self, response: object | Exception) -> None:
        self._response = response

    async def ainvoke(self, _prompt: str) -> object:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeChatModel:
    def __init__(
        self,
        *,
        tool_response: object | Exception,
        markdown_response: object | Exception,
    ) -> None:
        self.tool_response = tool_response
        self.markdown_response = markdown_response
        self.calls: list[str] = []

    def bind_tools(self, _tools: object, **_kwargs: object) -> _Runnable:
        self.calls.append("tools")
        return _Runnable(self.tool_response)

    async def ainvoke(self, _prompt: str) -> object:
        self.calls.append("markdown")
        response = self.markdown_response
        if isinstance(response, Exception):
            raise response
        return response


def _request() -> ModelCapabilityProbeRequest:
    return ModelCapabilityProbeRequest(
        model_name="demo-model",
        base_url="https://model.example/v1",
        api_key="test-api-key",
        temperature=0,
        timeout_seconds=30,
    )


def _tool_response() -> _ToolResponse:
    return _ToolResponse(
        tool_calls=[
            {
                "name": "start_data_analysis",
                "id": "call_probe_1",
                "args": {
                    "plan": {
                        "mode": "ready",
                        "requirements": [
                            {
                                "description": "验证完整分析合同",
                                "acceptance_criteria": ["返回标量与分组序列"],
                                "fulfillment": {
                                    "mode": "evidence",
                                    "assertions": [
                                        {
                                            "description": "验证结果字段",
                                            "result_columns": ["probe_value", "probe_group"],
                                            "sql_constraints": [
                                                {"kind": "source", "table": "probe_table"}
                                            ],
                                            "result_checks": [
                                                {"kind": "non_empty", "required": True}
                                            ],
                                            "claim_extractions": [
                                                {
                                                    "mode": "scalar",
                                                    "name": "探测标量",
                                                    "field": "probe_value",
                                                    "required": True,
                                                },
                                                {
                                                    "mode": "series",
                                                    "name": "探测序列",
                                                    "value_field": "probe_value",
                                                    "dimension_fields": ["probe_group"],
                                                },
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                        "execution_constraints": {
                            "forbidden_tools": [],
                            "required_artifacts": [
                                {
                                    "kind": "markdown",
                                    "minimum_count": 1,
                                    "description": "验证报告产物约束",
                                }
                            ],
                            "forbidden_artifact_kinds": [],
                        },
                    }
                },
            }
        ]
    )


def test_opening_probe_prompt_matches_execution_constraints_contract() -> None:
    assert "requirements 只含一个 evidence 目标" in _TOOL_PROBE_PROMPT
    assert "显式 mode=scalar" in _TOOL_PROBE_PROMPT
    assert "显式 kind=source" in _TOOL_PROBE_PROMPT
    assert "kind=markdown" in _TOOL_PROBE_PROMPT
    assert "不要依赖 mode 或 kind 默认值" in _TOOL_PROBE_PROMPT


@pytest.mark.asyncio
async def test_probe_prefers_direct_markdown_after_valid_native_tool_call() -> None:
    model = _FakeChatModel(
        tool_response=_tool_response(),
        markdown_response=_TextResponse("# 能力探测\n\n探测通过。"),
    )
    probe = LangChainModelCapabilityProbe(lambda _request: model)

    result = await probe.probe(_request())

    assert result.tool_calling_supported is True
    assert result.final_output_mode == "markdown"
    assert model.calls == ["tools", "markdown"]


@pytest.mark.asyncio
async def test_probe_falls_back_to_submit_answer_when_markdown_probe_fails() -> None:
    model = _FakeChatModel(
        tool_response=_tool_response(),
        markdown_response=RuntimeError("markdown unsupported"),
    )
    probe = LangChainModelCapabilityProbe(lambda _request: model)

    result = await probe.probe(_request())

    assert result.tool_calling_supported is True
    assert result.final_output_mode == "submit_answer"
    assert model.calls == ["tools", "markdown"]


@pytest.mark.asyncio
async def test_probe_rejects_single_line_markdown_and_uses_submit_answer() -> None:
    model = _FakeChatModel(
        tool_response=_tool_response(),
        markdown_response=_TextResponse("探测通过，但没有换行"),
    )
    probe = LangChainModelCapabilityProbe(lambda _request: model)

    result = await probe.probe(_request())

    assert result.tool_calling_supported is True
    assert result.final_output_mode == "submit_answer"
    assert model.calls == ["tools", "markdown"]


@pytest.mark.asyncio
async def test_probe_rejects_missing_tool_call_before_response_format_checks() -> None:
    model = _FakeChatModel(
        tool_response=_ToolResponse(tool_calls=[]),
        markdown_response=RuntimeError("must not be called"),
    )
    probe = LangChainModelCapabilityProbe(lambda _request: model)

    result = await probe.probe(_request())

    assert result.tool_calling_supported is False
    assert result.final_output_mode is None
    assert model.calls == ["tools"]


@pytest.mark.asyncio
async def test_probe_rejects_invalid_opening_plan() -> None:
    model = _FakeChatModel(
        tool_response=_ToolResponse(
            tool_calls=[{"name": "start_data_analysis", "id": "call_probe_1", "args": {"plan": {}}}]
        ),
        markdown_response=RuntimeError("must not be called"),
    )
    probe = LangChainModelCapabilityProbe(lambda _request: model)

    result = await probe.probe(_request())

    assert result.tool_calling_supported is False
    assert result.final_output_mode is None
    assert model.calls == ["tools"]
