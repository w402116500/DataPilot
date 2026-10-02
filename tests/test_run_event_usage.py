"""模型客户端真实 token 用量捕获与事件载荷校验的 focused 测试。"""

from __future__ import annotations

from typing import Any

import pytest
from agent_runtime.contracts import (
    AgentFailure,
    AnalysisOutcome,
    ModelUsageSummary,
)
from application.model_runtime import OpenAICompatibleModelClient, RunDeadline
from contracts.run_events import RunEventType
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import tool
from runtime.run_event_pipeline import _validate_payload


class FakeCancellation:
    """测试中可手动发出取消的最小信号。"""

    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class _UsageToolChatModel:
    """按脚本返回带（或不带）usage_metadata 的 tool calling 消息。"""

    def __init__(self, responses: list[dict[str, Any] | None]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    async def ainvoke(self, messages):
        del messages
        self.calls += 1
        usage = self._responses.pop(0) if self._responses else None
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"call_{self.calls}",
                    "name": "run_sql_readonly",
                    "args": {"sql": "SELECT 1"},
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage,
        )


@tool("run_sql_readonly")
def _usage_test_sql_tool(sql: str) -> str:
    """测试用只读 SQL Tool。"""

    del sql
    return "unused"


def _client(chat_model: Any) -> OpenAICompatibleModelClient:
    return OpenAICompatibleModelClient(
        model_name="demo-model",
        base_url="https://model.example/v1",
        api_key="secret-value",
        temperature=0.2,
        deadline=RunDeadline(60),
        chat_model=chat_model,
    )


async def _invoke(client: OpenAICompatibleModelClient) -> AIMessage:
    response = await client.invoke_with_tools(
        [HumanMessage(content="分析销售额")],
        [_usage_test_sql_tool],
        FakeCancellation(),
    )
    assert isinstance(response, AIMessage)
    return response


async def test_model_client_accumulates_usage_and_resets_turn_delta() -> None:
    chat = _UsageToolChatModel(
        [
            {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ]
    )
    client = _client(chat)

    await _invoke(client)
    assert client.usage_totals == ModelUsageSummary(
        input_tokens=100, output_tokens=20, total_tokens=120, model_calls=1
    )
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 120

    await _invoke(client)
    assert client.usage_totals == ModelUsageSummary(
        input_tokens=110, output_tokens=25, total_tokens=135, model_calls=2
    )
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 15

    client.reset_turn_usage()
    assert client.last_usage is None
    assert client.usage_totals.model_calls == 2


async def test_model_client_skips_usage_when_provider_omits_it() -> None:
    chat = _UsageToolChatModel([None])
    client = _client(chat)

    await _invoke(client)
    assert client.usage_totals == ModelUsageSummary()
    assert client.last_usage is None


def test_model_client_accepts_legacy_token_usage_metadata() -> None:
    chat = _UsageToolChatModel([])
    client = _client(chat)
    response = AIMessage(content="ok")
    response.response_metadata = {
        "token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    }

    client._record_usage(response)

    assert client.usage_totals == ModelUsageSummary(
        input_tokens=7, output_tokens=3, total_tokens=10, model_calls=1
    )


async def test_model_client_stream_usage_recorded_once_at_stream_end() -> None:
    class _UsageStreamingChatModel:
        def __init__(self) -> None:
            self.calls: list[list[object]] = []

        def astream(self, messages):
            self.calls.append(list(messages))

            async def stream():
                yield AIMessageChunk(content="结论")
                yield AIMessageChunk(
                    content="",
                    usage_metadata={"input_tokens": 50, "output_tokens": 8, "total_tokens": 58},
                )

            return stream()

    client = _client(_UsageStreamingChatModel())
    stream = await client.generate_final_answer_stream(
        [HumanMessage(content="生成答案")], FakeCancellation()
    )
    assert not isinstance(stream, AgentFailure)
    assert [chunk async for chunk in stream] == ["结论"]
    assert client.usage_totals == ModelUsageSummary(
        input_tokens=50, output_tokens=8, total_tokens=58, model_calls=1
    )
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 58


def _turn_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "turn_no": 1,
        "elapsed_ms": 12,
        "status": "completed",
        "action_kind": "tool_call",
        "tool_names": "run_sql_readonly",
        "tool_call_count": 1,
    }
    payload.update(overrides)
    return payload


def test_turn_completed_accepts_grouped_usage_fields() -> None:
    payload = _turn_payload(
        usage_input_tokens=100,
        usage_output_tokens=20,
        usage_total_tokens=120,
    )
    _validate_payload(RunEventType.AGENT_TURN_COMPLETED, payload)


def test_turn_completed_rejects_partial_usage_fields() -> None:
    payload = _turn_payload(usage_input_tokens=100, usage_output_tokens=20)
    with pytest.raises(ValueError, match="成组出现"):
        _validate_payload(RunEventType.AGENT_TURN_COMPLETED, payload)


def test_answer_ready_requires_tokens_alongside_model_calls() -> None:
    base = {
        "assistant_message_id": "message_1",
        "artifact_count": 0,
        "evidence_count": 0,
        "answer_format": "markdown",
        "completion_kind": "completed",
        "claim_audit_summary_json": "[]",
        "claim_audit_truncated": False,
        "answer_data_freshness": "current_schema",
        "historical_context_injected": False,
        "historical_summary_count": 0,
    }
    _validate_payload(
        RunEventType.ANSWER_READY,
        {
            **base,
            "usage_input_tokens": 1,
            "usage_output_tokens": 2,
            "usage_total_tokens": 3,
            "usage_model_calls": 4,
        },
    )
    with pytest.raises(ValueError, match="缺少用量字段"):
        _validate_payload(RunEventType.ANSWER_READY, {**base, "usage_model_calls": 4})


def test_outcome_usage_payload_omits_incomplete_usage() -> None:
    from runtime.run_finalizer import _outcome_usage_payload

    complete = AnalysisOutcome(
        answer="答案",
        usage_summary=ModelUsageSummary(
            input_tokens=1, output_tokens=2, total_tokens=3, model_calls=4
        ),
    )
    assert _outcome_usage_payload(complete) == {
        "usage_input_tokens": 1,
        "usage_output_tokens": 2,
        "usage_total_tokens": 3,
        "usage_model_calls": 4,
    }
    assert _outcome_usage_payload(AnalysisOutcome(answer="答案")) == {}
    partial_usage = AnalysisOutcome(
        answer="答案",
        usage_summary=ModelUsageSummary(model_calls=0),
    )
    assert _outcome_usage_payload(partial_usage) == {}
