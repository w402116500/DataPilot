"""阶段四外部适配器的脱网边界测试。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import application.model_runtime as model_runtime
import pytest
from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    AnalysisPlanFinalizationRequest,
    AnalysisPlanInvalidFailure,
    AnalysisPlanningDraft,
    AnalysisWarning,
    ConversationContext,
    DataLinkExploreCommand,
    FinalMarkdownPayload,
    HistoricalAnswerSummary,
    OpeningValidationIssue,
    RecentUserTurnProjection,
    RunOpeningAttemptSummary,
    RunOpeningDecision,
    RunOpeningInvalidFailure,
    SchemaLoadRequest,
    SqlExecutionFailure,
    SqlExecutionRequest,
    StartDataAnalysisArguments,
    WarningCode,
)
from agent_runtime.conversation_context import (
    datasource_identity,
    opening_context_projection,
    opening_repair_projection,
)
from agent_runtime.run_opening import (
    _mode_transition_issue,
    _plan_skeleton,
)
from agent_runtime.run_opening import (
    open_run as resolve_run_opening,
)
from application.agent_ports import DataGatewayAgentPort, DataLinkMcpPort
from application.model_runtime import (
    OpenAICompatibleModelClient,
    RunDeadline,
    build_openai_compatible_chat_model,
)
from application.runtime_wait import RuntimeWaitTimedOut, await_runtime_call
from contracts.datasources import DataSourceRead, SchemaSummaryRead, SqlExecutionRead
from contracts.runs import ModelRuntimeSnapshot
from contracts.status import DataSourceStatus, DataSourceType
from data_gateway.exceptions import QueryFailedError, QueryTimeoutError, SqlGuardBlockedError
from data_gateway.types import SqlGuardIssue
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from mcp.types import CallToolResult, TextContent


class FakeCancellation:
    """测试中可手动发出取消的最小信号。"""

    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


@pytest.mark.asyncio
async def test_runtime_wait_does_not_block_on_an_uncooperative_cancel() -> None:
    release = asyncio.Event()

    async def uncooperative_call() -> int:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)
            return 1
        return 1

    started_at = time.monotonic()
    with pytest.raises(RuntimeWaitTimedOut):
        await await_runtime_call(
            uncooperative_call(),
            cancellation=FakeCancellation(),
            timeout_seconds=0.01,
        )

    assert time.monotonic() - started_at < 0.3
    release.set()


class FakeDataSourceService:
    """只记录 Graph Gateway Port 的调用，不访问数据库或本地文件。"""

    def __init__(self, *, source: DataSourceRead, execution: SqlExecutionRead | Exception) -> None:
        self.source = source
        self.execution = execution
        self.sql_calls: list[dict[str, object]] = []

    async def get(self, datasource_id: str) -> DataSourceRead:
        assert datasource_id == self.source.id
        return self.source

    async def run_agent_sql(self, **kwargs: object) -> SqlExecutionRead:
        self.sql_calls.append(kwargs)
        if isinstance(self.execution, Exception):
            raise self.execution
        return self.execution


class RecordingMcpSession:
    """记录客户端会话实际收到的流，避免把会话 ID 获取函数误传为超时。"""

    def __init__(self, *streams: object) -> None:
        self.streams = streams
        self.initialized = False
        self.tool_calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> RecordingMcpSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialize(self) -> None:
        self.initialized = True

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.tool_calls.append((name, arguments))
        return CallToolResult(content=[TextContent(type="text", text="{}")])


def _source(*, status: DataSourceStatus = DataSourceStatus.READY) -> DataSourceRead:
    now = datetime.now(UTC)
    return DataSourceRead(
        id="datasource_1",
        name="Demo",
        description=None,
        type=DataSourceType.CSV,
        status=status,
        schema_revision=1,
        schema_summary=SchemaSummaryRead(
            datasource_id="datasource_1",
            dialect="duckdb",
            tables=[
                {
                    "name": "dataset",
                    "columns": [{"name": "value", "type": "INTEGER", "nullable": False}],
                }
            ],
        ),
        datalink_build_id="build_1",
        datalink_graph_version="graph_1",
        last_error_code=None,
        last_error_message=None,
        last_test_at=now,
        created_at=now,
        updated_at=now,
    )


def _opening_context(
    question: str,
    context: ConversationContext | None = None,
):
    source = _source()
    return opening_context_projection(
        context=context or ConversationContext(),
        question=question,
        identity=datasource_identity(
            name=source.name,
            source_type=source.type.value,
            schema=source.schema_summary,
            schema_revision=source.schema_revision,
        ),
        schema=source.schema_summary,
    )


class RecordingChatModel:
    """模拟 LangChain Runnable，只记录 Runtime 选择的协议而不访问真实模型。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.mode = ""
        self.structured_schema = None
        self.opening_response: AIMessage = AIMessage(content="普通回答")
        self.finalization_response: AIMessage = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_finalize",
                    "name": "finalize_analysis_plan",
                    "args": {
                        "plan": {
                            "mode": "ready",
                            "requirements": [
                                {
                                    "description": "说明当前字段",
                                    "acceptance_criteria": ["只使用当前 Schema"],
                                    "fulfillment": {
                                        "mode": "context_only",
                                        "sources": ["schema"],
                                    },
                                }
                            ],
                        }
                    },
                    "type": "tool_call",
                }
            ],
        )
        self.finalization_responses: list[AIMessage] | None = None

    def bind_tools(self, tools, **kwargs):
        self.calls.append(("bind_tools", ([item.name for item in tools], kwargs)))
        self.mode = "tool_calling"
        return self

    def with_structured_output(self, schema, **kwargs):
        self.calls.append(("with_structured_output", (schema, kwargs)))
        self.mode = "json_schema"
        self.structured_schema = schema
        return self

    def bind(self, **kwargs):
        self.calls.append(("bind", kwargs))
        self.mode = "json_object"
        return self

    async def ainvoke(self, messages):
        self.calls.append(("ainvoke", list(messages)))
        if self.mode == "" or (self.mode == "tool_calling" and self.calls[-2][0] != "bind_tools"):
            return AIMessage(content="Markdown 答案")
        if self.mode == "json_schema":
            return {"markdown": "Schema 答案"}
        if self.mode == "json_object":
            return AIMessage(content=json.dumps({"markdown": "JSON 答案"}))
        tool_names = self.calls[-2][1][0]
        if tool_names == ["start_data_analysis"]:
            return self.opening_response
        if tool_names == ["finalize_analysis_plan"]:
            if self.finalization_responses:
                return self.finalization_responses.pop(0)
            return self.finalization_response
        if tool_names == ["submit_answer"]:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_submit",
                        "name": "submit_answer",
                        "args": {"markdown": "Tool 答案"},
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_sql",
                    "name": "run_sql_readonly",
                    "args": {"sql": "SELECT 1"},
                    "type": "tool_call",
                }
            ],
        )


class StreamingChatModel:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.calls: list[list[object]] = []

    def astream(self, messages):
        self.calls.append(list(messages))

        async def stream():
            for chunk in self.chunks:
                yield chunk

        return stream()


class FailingStreamingChatModel:
    def astream(self, _messages):
        raise RuntimeError("stream setup failed")


@tool("run_sql_readonly")
def _test_sql_tool(sql: str) -> str:
    """测试用只读 SQL Tool。"""

    del sql
    return "unused"


@tool("commit_analysis_claims")
def _test_commit_tool(claims: list[dict[str, object]]) -> str:
    """测试用内部结论提交 Tool。"""

    del claims
    return "unused"


def _client(
    chat_model: RecordingChatModel,
    deadline: RunDeadline | None = None,
    model_context: ModelRuntimeSnapshot | None = None,
):
    return OpenAICompatibleModelClient(
        model_name="demo-model",
        base_url="https://model.example/v1",
        api_key="secret-value",
        temperature=0.2,
        deadline=deadline or RunDeadline(60),
        chat_model=chat_model,
        model_context=model_context,
    )


def test_siliconflow_tool_turns_preserve_returned_reasoning_content() -> None:
    model = build_openai_compatible_chat_model(
        model_name="demo-model",
        base_url="https://api.siliconflow.cn/v1",
        api_key="secret-value",
        temperature=0,
        timeout=30,
    )
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "先核对合同，再修复 SQL。"},
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT 1"},
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content='{"status":"failed","error_code":"ANALYSIS_SQL_CONTRACT_INVALID"}',
                tool_call_id="call_1",
            ),
        ]
    )

    assert payload["messages"][0]["reasoning_content"] == "先核对合同，再修复 SQL。"
    assert payload["messages"][1]["tool_call_id"] == "call_1"


def test_siliconflow_tool_turns_use_placeholder_when_reasoning_is_missing() -> None:
    model = build_openai_compatible_chat_model(
        model_name="demo-model",
        base_url="https://api.siliconflow.cn/v1",
        api_key="secret-value",
        temperature=0,
        timeout=30,
    )
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT 1"},
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )

    assert payload["messages"][0]["reasoning_content"] == " "


def test_deepseek_tool_turns_use_the_same_reasoning_compatibility() -> None:
    model = build_openai_compatible_chat_model(
        model_name="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        api_key="secret-value",
        temperature=0,
        timeout=30,
    )
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "继续上一轮思考。"},
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT 1"},
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )

    assert payload["messages"][0]["reasoning_content"] == "继续上一轮思考。"


def test_other_openai_compatible_hosts_keep_standard_message_shape() -> None:
    model = build_openai_compatible_chat_model(
        model_name="demo-model",
        base_url="https://model.example/v1",
        api_key="secret-value",
        temperature=0,
        timeout=30,
    )
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT 1"},
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )

    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.asyncio
async def test_model_runtime_separates_data_tool_calling_and_final_output_protocols() -> None:
    chat = RecordingChatModel()
    client = _client(chat)
    messages = [HumanMessage(content="分析销售额")]

    tool_response = await client.invoke_with_tools(messages, [_test_sql_tool], FakeCancellation())
    markdown_answer = await client.generate_final_answer(messages, "markdown", FakeCancellation())
    schema_answer = await client.generate_final_answer(messages, "json_schema", FakeCancellation())
    object_answer = await client.generate_final_answer(messages, "json_object", FakeCancellation())
    tool_answer = await client.generate_final_answer(messages, "submit_answer", FakeCancellation())

    assert isinstance(tool_response, AIMessage)
    assert tool_response.tool_calls[0]["id"] == "call_sql"
    assert markdown_answer == FinalMarkdownPayload(markdown="Markdown 答案")
    assert schema_answer == FinalMarkdownPayload(markdown="Schema 答案")
    assert object_answer == FinalMarkdownPayload(markdown="JSON 答案")
    assert tool_answer == FinalMarkdownPayload(markdown="Tool 答案")
    assert chat.calls[0] == (
        "bind_tools",
        (["run_sql_readonly"], {"strict": True, "parallel_tool_calls": False}),
    )
    assert chat.calls[2][0] == "ainvoke"
    assert chat.calls[3][0] == "with_structured_output"
    assert chat.calls[5] == ("bind", {"response_format": {"type": "json_object"}})
    assert chat.calls[7] == (
        "bind_tools",
        (["submit_answer"], {"strict": True, "parallel_tool_calls": False}),
    )


@pytest.mark.asyncio
async def test_model_runtime_streams_markdown_chunks_without_structured_output() -> None:
    chat = StreamingChatModel(
        [AIMessageChunk(content="## 结"), AIMessageChunk(content="论\n\n已完成。")]
    )
    client = _client(chat)

    stream = await client.generate_final_answer_stream(
        [HumanMessage(content="生成答案")], FakeCancellation()
    )

    assert not isinstance(stream, AgentFailure)
    assert [chunk async for chunk in stream] == ["## 结", "论\n\n已完成。"]
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_model_runtime_streaming_first_token_timeout_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = StreamingChatModel([])

    async def timeout_request(operation, *, cancellation, timeout_seconds):
        del cancellation, timeout_seconds
        operation.close()
        raise model_runtime.RuntimeWaitTimedOut

    monkeypatch.setattr(model_runtime, "await_runtime_call", timeout_request)
    client = _client(chat, RunDeadline(600))
    stream = await client.generate_final_answer_stream(
        [HumanMessage(content="生成答案")], FakeCancellation()
    )

    assert not isinstance(stream, AgentFailure)
    assert [item async for item in stream] == [
        AgentFailure(
            code=AgentErrorCode.FINAL_ANSWER_TIMEOUT, message="最终答案模型请求超过本阶段时限"
        )
    ]


@pytest.mark.asyncio
async def test_model_runtime_streaming_idle_timeout_after_first_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = StreamingChatModel([AIMessageChunk(content="首段")])
    calls = 0

    async def timeout_after_first(operation, *, cancellation, timeout_seconds):
        nonlocal calls
        del cancellation, timeout_seconds
        calls += 1
        if calls == 1:
            return await operation
        operation.close()
        raise model_runtime.RuntimeWaitTimedOut

    monkeypatch.setattr(model_runtime, "await_runtime_call", timeout_after_first)
    client = _client(chat, RunDeadline(600))
    stream = await client.generate_final_answer_stream(
        [HumanMessage(content="生成答案")], FakeCancellation()
    )

    assert not isinstance(stream, AgentFailure)
    assert [item async for item in stream] == [
        "首段",
        AgentFailure(
            code=AgentErrorCode.FINAL_ANSWER_TIMEOUT, message="最终答案模型请求超过本阶段时限"
        ),
    ]


@pytest.mark.asyncio
async def test_model_runtime_streaming_setup_failure_is_structured() -> None:
    client = _client(FailingStreamingChatModel())
    stream = await client.generate_final_answer_stream(
        [HumanMessage(content="生成答案")], FakeCancellation()
    )

    assert not isinstance(stream, AgentFailure)
    assert [item async for item in stream] == [
        AgentFailure(code=AgentErrorCode.MODEL_REQUEST_FAILED, message="模型请求失败")
    ]


def _analysis_opening_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_opening",
                "name": "start_data_analysis",
                "args": {
                    "plan": {
                        "mode": "ready",
                        "requirements": [
                            {
                                "description": "说明当前数据结构",
                                "acceptance_criteria": ["只使用当前 Schema"],
                                "fulfillment": {
                                    "mode": "context_only",
                                    "sources": ["schema"],
                                },
                            }
                        ],
                    }
                },
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.asyncio
async def test_model_runtime_opening_accepts_plain_text_as_general_task() -> None:
    chat = RecordingChatModel()

    result = await _client(chat).open_run(
        "什么是同比？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("什么是同比？"),
    )

    assert result.protocol_id == "general-task"
    assert result.answer == "普通回答"
    assert chat.calls[0] == (
        "bind_tools",
        (["start_data_analysis"], {"strict": True, "parallel_tool_calls": False}),
    )
    prompt = "\n".join(message.content for message in chat.calls[1][1])
    assert "先且只先判断回答是否必须读取当前数据" in prompt
    assert "‘数据模型是什么？’、‘数据中的偏差可能来自哪里？’" in prompt
    assert "‘当前数据模型是什么？’、‘这份数据中的偏差可能来自哪里？’" in prompt
    assert "只有用户范围、指标、比较基线或交付格式缺失" in prompt
    assert "宽泛、多个指标、多步骤或正式报告任务可以先使用 discovery" in prompt
    assert "不做 SQL 设计、字段校验、结果解释或产物生成" in prompt
    assert "不预设任何具体业务实体、客户、订单或默认指标" in prompt
    assert "needs_semantic_context" in prompt
    assert "省略 semantic_request" in prompt
    assert "语义缺口" in prompt
    assert "点名分析对象" in prompt
    assert "explore_datalink" in prompt
    assert "若该工具开放" in prompt
    assert "分析当前客户" not in prompt
    assert "aggregate" not in prompt
    assert "chart_generated" not in prompt


@pytest.mark.asyncio
async def test_opening_budget_is_checked_before_provider_io() -> None:
    chat = RecordingChatModel()
    context = ModelRuntimeSnapshot(
        profile_id="profile_budget",
        provider="openai-compatible",
        model_name="demo-model",
        base_url="https://model.example/v1",
        temperature=0,
        run_timeout_seconds=60,
        final_output_mode="markdown",
        model_capability_fingerprint="b" * 64,
        context_window_tokens=1_024,
        output_budget_tokens=512,
        system_budget_tokens=0,
        tool_schema_cost=0,
        safety_margin_tokens=0,
    )

    result = await _client(chat, model_context=context).open_run(
        "什么是同比？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("什么是同比？"),
    )

    assert isinstance(result, AgentFailure)
    assert result.code is AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED
    assert [call[0] for call in chat.calls] == []


@pytest.mark.asyncio
async def test_request_budget_estimate_is_safe_and_records_source() -> None:
    chat = RecordingChatModel()
    context = ModelRuntimeSnapshot(
        profile_id="profile_budget",
        provider="openai-compatible",
        model_name="demo-model",
        base_url="https://model.example/v1",
        temperature=0,
        run_timeout_seconds=60,
        final_output_mode="markdown",
        model_capability_fingerprint="d" * 64,
        context_window_tokens=8_192,
        output_budget_tokens=512,
        system_budget_tokens=128,
        tool_schema_cost=64,
        safety_margin_tokens=32,
    )
    client = _client(chat, model_context=context)

    result = await client.open_run(
        "什么是同比？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("什么是同比？"),
    )

    assert result.protocol_id == "general-task"
    assert client.last_request_budget is not None
    metadata = client.last_request_budget.as_metadata()
    assert metadata["model_input_chars"] > 0
    assert metadata["model_input_tokens"] > 0
    assert metadata["estimate_source"] == "utf8_estimate"
    assert "content" not in metadata


def test_empty_tools_do_not_reserve_tool_schema_tokens() -> None:
    client = _client(RecordingChatModel())
    messages = [HumanMessage(content="说明当前表结构")]
    empty = client._estimate_request_budget(messages, [])
    with_tools = client._estimate_request_budget(messages, [_test_sql_tool])

    assert empty.estimated_total_tokens + 8_192 <= with_tools.estimated_total_tokens


@pytest.mark.asyncio
async def test_local_agent_budget_failure_does_not_consume_context_retry() -> None:
    chat = RecordingChatModel()
    context = ModelRuntimeSnapshot(
        profile_id="profile_budget",
        provider="openai-compatible",
        model_name="demo-model",
        base_url="https://model.example/v1",
        temperature=0,
        run_timeout_seconds=60,
        final_output_mode="markdown",
        model_capability_fingerprint="c" * 64,
        context_window_tokens=1_024,
        output_budget_tokens=512,
        system_budget_tokens=0,
        tool_schema_cost=0,
        safety_margin_tokens=0,
    )

    result = await _client(chat, model_context=context).invoke_with_tools(
        [HumanMessage(content="x" * 10_000)],
        [_test_sql_tool],
        FakeCancellation(),
    )

    assert isinstance(result, AgentFailure)
    assert result.code is AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED
    assert result.context_retry_allowed is False
    assert [call[0] for call in chat.calls] == []


def test_start_data_analysis_tool_doc_describes_opening_contract() -> None:
    description = model_runtime.start_data_analysis.description or ""

    assert "needs_semantic_context" in description
    assert "semantic_request" in description
    assert "evidence" in description
    assert "context_only" in description
    assert "blocked" in description
    assert "table.column" in description
    assert "discovery_scope" in description
    assert "表达式聚合" not in description
    assert "chart_generated" not in description
    assert "result_columns" not in description


def test_opening_prompts_teach_semantic_request_contract() -> None:
    opening = model_runtime._OPENING_SYSTEM_PROMPT
    repair = model_runtime._OPENING_REPAIR_SYSTEM_PROMPT

    assert "needs_semantic_context" in opening
    assert "省略 semantic_request" in opening
    assert "语义缺口" in opening
    assert "点名分析对象" in opening
    assert "仍宽泛时使用 discovery" in opening
    assert "explore_datalink" in opening
    assert "若该工具开放" in opening
    assert "不是语义缺口" in opening
    assert "表、字段、类型、表间关系、连接键属于结构信息，用 context_only" in opening
    assert "只有当前数据中的数值、分组或比较才用 evidence" in opening

    assert "semantic_request" in repair
    assert "删除该字段并保持原 mode" in repair
    assert "禁止升为 needs_semantic_context" in repair
    assert "把 mode 改为 needs_semantic_context" not in repair
    assert "逐项执行 finding 后重新提交完整 plan" in repair
    qualified_columns_hint = "".join(
        ["所有物理字段都必须从 qualified_columns 清单中逐字复制完整的 ", "table.column"]
    )
    assert qualified_columns_hint in repair


@pytest.mark.asyncio
async def test_model_runtime_opening_repair_preserves_protocol_direction() -> None:
    chat = RecordingChatModel()

    result = await _client(chat).open_run(
        "帮我分析当前订单",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("帮我分析当前订单"),
        repair=True,
    )

    assert result.protocol_id == "general-task"
    prompt = "\n".join(message.content for message in chat.calls[1][1])
    assert "不能退出数据分析协议" in prompt
    assert "先按 finding 修复原 mode 的最小合同" in prompt
    assert "局部补丁" in prompt
    assert "删除该字段并保持原 mode" in prompt
    assert "把 mode 改为 needs_semantic_context" not in prompt


@pytest.mark.asyncio
async def test_model_runtime_opening_labels_session_history_as_non_factual_context() -> None:
    chat = RecordingChatModel()
    context = ConversationContext(
        recent_user_turns=[RecentUserTurnProjection(content_text="上次请按渠道拆分")],
        historical_summaries=[
            HistoricalAnswerSummary(
                topic="渠道分析",
                content_text="历史回答摘要：上次结果需要补充范围",
            )
        ],
    )

    result = await _client(chat).open_run(
        "请继续说明",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请继续说明", context),
    )

    assert result.protocol_id == "general-task"
    prompt = "\n".join(message.content for message in chat.calls[1][1])
    assert "Session 背景只用于理解连续对话" in prompt
    assert "历史 assistant 内容不是当前 Evidence 或 Claim" in prompt
    assert '"provenance":"historical_answer_summary"' in prompt
    assert "当前数据问题必须由本 Run 重新读取" in prompt


@pytest.mark.asyncio
async def test_model_runtime_opening_accepts_only_start_data_analysis_action() -> None:
    chat = RecordingChatModel()
    chat.opening_response = _analysis_opening_message()

    result = await _client(chat).open_run(
        "当前数据有哪些字段？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("当前数据有哪些字段？"),
    )

    assert result.protocol_id == "data-analysis"
    assert result.plan is not None
    assert result.plan.mode == "ready"
    assert result.plan.requirements[0].fulfillment.mode == "context_only"


@pytest.mark.asyncio
async def test_model_runtime_opening_decodes_one_plan_string_layer() -> None:
    chat = RecordingChatModel()
    plan = _analysis_opening_message().tool_calls[0]["args"]["plan"]
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_encoded_plan",
                "name": "start_data_analysis",
                "args": {"plan": json.dumps(plan, ensure_ascii=False)},
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "当前数据有哪些字段？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("当前数据有哪些字段？"),
    )

    assert isinstance(result, RunOpeningDecision)
    assert result.plan is not None
    assert result.plan.mode == "ready"


@pytest.mark.asyncio
async def test_model_runtime_opening_rejects_nested_plan_string() -> None:
    chat = RecordingChatModel()
    plan = _analysis_opening_message().tool_calls[0]["args"]["plan"]
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_nested_plan",
                "name": "start_data_analysis",
                "args": {"plan": json.dumps(json.dumps(plan, ensure_ascii=False))},
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "当前数据有哪些字段？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("当前数据有哪些字段？"),
    )

    assert isinstance(result, RunOpeningInvalidFailure)
    assert result.validation_issues[0].error_type == "dict_type"
    assert result.attempt_summary is not None
    assert result.attempt_summary.trusted_mode is None


def test_opening_repair_mode_matrix_allows_only_ready_to_discovery() -> None:
    for first_mode in ("ready", "discovery", "clarification", "needs_semantic_context"):
        failure = RunOpeningInvalidFailure(
            message="invalid",
            attempt_summary=RunOpeningAttemptSummary(
                attempted_protocol="data-analysis",
                trusted_mode=first_mode,
            ),
        )
        for repaired_mode in ("ready", "discovery", "clarification", "needs_semantic_context"):
            plan_payload = {"mode": repaired_mode}
            if repaired_mode == "clarification":
                plan_payload["clarification"] = {
                    "question": "需要确认什么？",
                    "missing_items": ["data_scope"],
                }
            else:
                plan_payload["requirements"] = [
                    {
                        "description": "最小目标",
                        "acceptance_criteria": ["完成"],
                        "fulfillment": {
                            "mode": "context_only",
                            "sources": ["schema"],
                        },
                    }
                ]
                if repaired_mode == "needs_semantic_context":
                    plan_payload["semantic_request"] = {"query": "业务字段"}
                if repaired_mode == "discovery":
                    plan_payload["discovery_scope"] = {
                        "tables": ["dataset"],
                        "columns": ["dataset.value"],
                        "max_rows": 10,
                    }
            repaired = RunOpeningDecision(
                protocol_id="data-analysis",
                plan=AnalysisPlanningDraft.model_validate(plan_payload),
            )
            issue = _mode_transition_issue(failure, repaired)
            allowed = repaired_mode == first_mode or (
                first_mode == "ready" and repaired_mode == "discovery"
            )
            assert (issue is None) is allowed, (first_mode, repaired_mode, issue)


def test_invalid_opening_attempt_summary_is_the_repair_skeleton() -> None:
    failure = RunOpeningInvalidFailure(
        message="invalid",
        attempt_summary=RunOpeningAttemptSummary(
            attempted_protocol="data-analysis",
            trusted_mode="ready",
            requirement_count=2,
            fulfillment_count=2,
            assertion_count=3,
            artifact_count=1,
        ),
    )

    assert _plan_skeleton(failure) == {
        "protocol_id": "data-analysis",
        "mode": "ready",
        "requirement_count": 2,
        "fulfillment_count": 2,
        "assertion_count": 3,
        "artifact_count": 1,
        "allowed_action": "start_data_analysis",
    }


def test_start_data_analysis_schema_requires_every_const_discriminator() -> None:
    schema = model_runtime.start_data_analysis.args_schema.model_json_schema()
    tagged_definitions: set[str] = set()

    for name, definition in schema["$defs"].items():
        properties = definition.get("properties", {})
        for discriminator in ("mode", "kind"):
            property_schema = properties.get(discriminator, {})
            if "const" not in property_schema:
                continue
            tagged_definitions.add(name)
            assert discriminator in definition.get("required", []), name
            assert "default" not in property_schema, name

    assert {
        "AnalysisScalarClaimExtraction",
        "AnalysisSeriesClaimExtraction",
        "AnalysisEvidenceFulfillmentDraft",
        "AnalysisContextOnlyFulfillmentDraft",
        "AnalysisBlockedFulfillmentDraft",
    } <= tagged_definitions


@pytest.mark.asyncio
async def test_model_runtime_opening_repair_receives_safe_validation_issues() -> None:
    chat = RecordingChatModel()
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_invalid_opening",
                "name": "start_data_analysis",
                "args": {
                    "plan": {
                        "mode": "ready",
                        "requirements": [
                            {
                                "description": "统计客户数",
                                "acceptance_criteria": ["返回客户数"],
                                "fulfillment": {
                                    "mode": "evidence",
                                    "assertions": [
                                        {
                                            "description": "统计客户数",
                                            "claim_extractions": [
                                                {
                                                    "name": "客户数",
                                                    "field": "customer_count",
                                                    "required": True,
                                                }
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                    "semantic_request": None,
                },
                "type": "tool_call",
            }
        ],
    )
    client = _client(chat)

    first = await client.open_run(
        "分析当前客户",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("分析当前客户"),
    )

    assert isinstance(first, RunOpeningInvalidFailure)
    assert {issue.error_type for issue in first.validation_issues} == {
        "union_tag_not_found",
        "extra_forbidden",
    }
    assert any("标量为 scalar" in issue.action for issue in first.validation_issues)
    assert any("plan.semantic_request" in issue.action for issue in first.validation_issues)

    chat.opening_response = AIMessage(content="普通回答")
    repaired = await client.open_run(
        "分析当前客户",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("分析当前客户"),
        repair=True,
        repair_issues=first.validation_issues,
    )

    assert repaired.protocol_id == "general-task"
    repair_prompt = "\n".join(
        message.content for message in [call for call in chat.calls if call[0] == "ainvoke"][-1][1]
    )
    assert '"error_type":"union_tag_not_found"' in repair_prompt
    assert '"error_type":"extra_forbidden"' in repair_prompt
    assert "逐项执行 finding 后重新提交完整 plan" in repair_prompt
    qualified_columns_hint = "".join(
        ["所有物理字段都必须从 qualified_columns 清单中逐字复制完整的 ", "table.column"]
    )
    assert qualified_columns_hint in repair_prompt
    assert (
        "删除旧的非法字段，再从 qualified_columns 中逐字复制真实存在的 table.column"
        in repair_prompt
    )
    assert "customer_count" not in repair_prompt


@pytest.mark.asyncio
async def test_opening_repair_does_not_reintroduce_session_history() -> None:
    chat = RecordingChatModel()
    context = ConversationContext(
        recent_user_turns=[RecentUserTurnProjection(content_text="上一轮的无关内容")],
        historical_summaries=[
            HistoricalAnswerSummary(topic="旧主题", content_text="旧回答不应进入 Repair")
        ],
    )
    issue = OpeningValidationIssue(
        path="plan.mode",
        error_type="literal_error",
        repair_reason="shape_invalid",
        actual="枚举值不合法",
        expected="ready / clarification / discovery",
        rule="mode 必须是受限枚举",
        action="选择一个合法 mode",
    )

    result = await _client(chat).open_run(
        "请继续分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请继续分析当前数据", context),
        repair=True,
        repair_issues=[issue],
        repair_plan_skeleton={"protocol_id": "data-analysis", "mode": "ready"},
    )

    assert result.protocol_id == "general-task"
    repair_prompt = "\n".join(
        message.content
        for name, messages in chat.calls
        if name == "ainvoke"
        for message in messages
    )
    assert "上一轮的无关内容" not in repair_prompt
    assert "旧回答不应进入 Repair" not in repair_prompt
    assert '"path":"plan.mode"' in repair_prompt


def test_opening_repair_schema_is_limited_to_finding_candidates() -> None:
    schema = SchemaSummaryRead(
        datasource_id="datasource_1",
        dialect="sqlite",
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "customer_id", "type": "INTEGER", "nullable": True}],
            },
            {
                "name": "events",
                "columns": [{"name": "customer_id", "type": "INTEGER", "nullable": True}],
            },
            {
                "name": "internal_table",
                "columns": [{"name": "secret", "type": "TEXT", "nullable": True}],
            },
        ],
    )
    issue = OpeningValidationIssue(
        path="plan.requirements[0].fulfillment.assertions[0].source_tables[0]",
        error_type="ambiguous_column",
        repair_reason="schema_reference_invalid",
        actual="customer_id",
        expected="请选择 orders.customer_id, events.customer_id",
        action="选择完整的 table.column",
    )

    projection = opening_repair_projection(
        question="分析客户",
        schema=schema,
        validation_issues=[issue],
        plan_skeleton={"protocol_id": "data-analysis", "mode": "ready"},
    )

    assert {table.name for table in projection.schema_index.tables} == {"orders", "events"}
    assert all(
        [column.name for column in table.columns] == ["customer_id"]
        for table in projection.schema_index.tables
    )
    assert projection.qualified_columns == ["orders.customer_id", "events.customer_id"]

    table_issue = issue.model_copy(
        update={
            "error_type": "unknown_table",
            "actual": "missing_table",
            "expected": "当前冻结 Schema 中存在的表名",
        }
    )
    table_projection = opening_repair_projection(
        question="分析客户",
        schema=schema,
        validation_issues=[table_issue],
    )
    assert {table.name for table in table_projection.schema_index.tables} == {
        "orders",
        "events",
        "internal_table",
    }
    assert all(not table.columns for table in table_projection.schema_index.tables)


def test_opening_repair_keeps_schema_for_non_reference_contract_findings() -> None:
    schema = SchemaSummaryRead(
        datasource_id="datasource_1",
        dialect="sqlite",
        tables=[
            {
                "name": "customers",
                "columns": [
                    {"name": "customer_id", "type": "INTEGER", "nullable": True},
                    {"name": "channel", "type": "TEXT", "nullable": False},
                ],
            },
            {
                "name": "orders",
                "columns": [{"name": "order_id", "type": "INTEGER", "nullable": True}],
            },
        ],
    )
    issue = OpeningValidationIssue(
        path="plan.execution_constraints.required_artifacts",
        error_type="value_error",
        repair_reason="artifact_conflict",
        actual="已提供",
        expected="discovery 不得包含正式 Artifact",
        action="删除 required_artifacts 后重新提交完整 plan。",
    )

    projection = opening_repair_projection(
        question="分析当前客户并生成 Markdown 报告",
        schema=schema,
        validation_issues=[issue],
        plan_skeleton={"protocol_id": "data-analysis", "mode": "discovery"},
    )

    assert [table.name for table in projection.schema_index.tables] == ["customers", "orders"]
    assert projection.qualified_columns == [
        "customers.customer_id",
        "customers.channel",
        "orders.order_id",
    ]


@pytest.mark.asyncio
async def test_run_opening_preserves_repair_timeout_instead_of_masking_it() -> None:
    issue = OpeningValidationIssue(
        path="plan.requirements[0].fulfillment.evidence.assertions[0].claim_extractions[0]",
        error_type="union_tag_not_found",
        action="显式添加 mode。",
    )

    class RepairTimeoutModel:
        def __init__(self) -> None:
            self.calls = []

        async def open_run(
            self,
            _question,
            _schema,
            _cancellation,
            *,
            opening_context=None,
            repair=False,
            repair_issues=(),
            repair_plan_skeleton=None,
        ):
            self.calls.append((repair, tuple(repair_issues), opening_context))
            if not repair:
                return RunOpeningInvalidFailure(
                    message="Opening 参数非法",
                    retryable=True,
                    validation_issues=[issue],
                )
            return AgentFailure(
                code=AgentErrorCode.RUN_OPENING_TIMEOUT,
                message="Run Opening 模型请求超过阶段时限",
            )

    model = RepairTimeoutModel()
    result = await resolve_run_opening(
        model,
        question="分析当前客户",
        schema=_source().schema_summary,
        cancellation=FakeCancellation(),
        opening_context=_opening_context("分析当前客户"),
    )

    assert result.decision.code is AgentErrorCode.RUN_OPENING_TIMEOUT
    assert result.opening_model_calls == 2
    assert result.opening_repair_calls == 1
    assert model.calls[1][1] == (issue,)


@pytest.mark.asyncio
async def test_run_opening_uses_repair_exhausted_only_for_second_invalid_shape() -> None:
    class InvalidTwiceModel:
        async def open_run(
            self,
            _question,
            _schema,
            _cancellation,
            *,
            opening_context=None,
            repair=False,
            repair_issues=(),
            repair_plan_skeleton=None,
        ):
            del opening_context, repair, repair_issues, repair_plan_skeleton
            return RunOpeningInvalidFailure(message="Opening 参数非法", retryable=True)

    result = await resolve_run_opening(
        InvalidTwiceModel(),
        question="分析当前客户",
        schema=_source().schema_summary,
        cancellation=FakeCancellation(),
        opening_context=_opening_context("分析当前客户"),
    )

    assert result.decision.code is AgentErrorCode.MODEL_OUTPUT_REPAIR_EXHAUSTED
    assert result.opening_model_calls == 2
    assert result.opening_repair_calls == 1


@pytest.mark.asyncio
async def test_run_opening_repairs_semantically_invalid_plan_once() -> None:
    invalid = RunOpeningDecision(
        protocol_id="data-analysis",
        plan=AnalysisPlanningDraft(
            mode="ready",
            requirements=[
                {
                    "description": "统计客户数",
                    "acceptance_criteria": ["返回客户数"],
                    "fulfillment": {
                        "mode": "evidence",
                        "assertions": [
                            {
                                "description": "统计客户数",
                                "source_tables": ["orders"],
                                "result_columns": ["customer_count"],
                                "sql_constraints": [
                                    {"kind": "source", "table": "orders"},
                                    {"kind": "column", "column": "missing_column"},
                                ],
                                "claim_extractions": [
                                    {
                                        "mode": "scalar",
                                        "name": "客户数",
                                        "field": "customer_count",
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        ),
    )
    repaired = RunOpeningDecision(
        protocol_id="data-analysis",
        plan=AnalysisPlanningDraft(
            mode="ready",
            requirements=[
                {
                    "description": "说明当前数据结构",
                    "acceptance_criteria": ["只使用当前 Schema"],
                    "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                }
            ],
        ),
    )

    class SemanticRepairModel:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        async def open_run(
            self,
            _question,
            _schema,
            _cancellation,
            *,
            opening_context=None,
            repair=False,
            repair_issues=(),
            repair_plan_skeleton=None,
        ):
            del opening_context, repair_plan_skeleton
            self.calls.append(repair)
            if not repair:
                assert repair_issues == ()
                return invalid
            assert repair_issues
            return repaired

    from agent_runtime.analysis_planning import materialize_analysis_plan

    model = SemanticRepairModel()
    result = await resolve_run_opening(
        model,
        question="统计客户数",
        schema=_source().schema_summary,
        cancellation=FakeCancellation(),
        opening_context=_opening_context("统计客户数"),
        plan_validator=lambda draft: (
            validation
            if isinstance(
                validation := materialize_analysis_plan(draft, _source().schema_summary),
                AgentFailure,
            )
            else None
        ),
    )

    assert result.decision == repaired
    assert result.opening_model_calls == 2
    assert result.opening_repair_calls == 1
    assert result.validation_issues[0].error_type == "unknown_table"
    assert result.validation_issues[0].repair_reason == "schema_reference_invalid"
    assert result.validation_issues[0].path.startswith("requirements[0]")
    assert model.calls == [False, True]


@pytest.mark.asyncio
async def test_model_runtime_opening_request_failure_logs_only_safe_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingOpeningChatModel(RecordingChatModel):
        async def ainvoke(self, _messages):
            raise RuntimeError("provider response must not be logged")

    caplog.set_level("WARNING", logger="application.model_runtime")

    result = await _client(FailingOpeningChatModel()).open_run(
        "当前有多少订单？",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("当前有多少订单？"),
    )

    assert result.code is AgentErrorCode.MODEL_REQUEST_FAILED
    record = next(
        item for item in caplog.records if item.message == "Run Opening model request failed"
    )
    assert record.error_code == AgentErrorCode.MODEL_REQUEST_FAILED.value
    assert record.error_type == "RuntimeError"
    assert "provider response must not be logged" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "expected_path", "expected_reason", "expected_actual", "expected_action"),
    [
        (
            {
                "mode": "discovery",
                "requirements": [
                    {
                        "description": "探索销售数据",
                        "acceptance_criteria": ["返回受限观察"],
                        "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                    }
                ],
            },
            "plan.discovery_scope",
            "discovery_scope_missing",
            "缺失",
            "discovery_scope",
        ),
        (
            {
                "mode": "clarification",
                "requirements": [
                    {
                        "description": "销售分析",
                        "acceptance_criteria": ["明确分析口径"],
                        "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                    }
                ],
                "clarification": {
                    "question": "请补充指标",
                    "missing_items": ["metric"],
                },
            },
            "plan.requirements",
            "shape_invalid",
            "1 项",
            "删除 requirements",
        ),
        (
            {
                "mode": "ready",
                "requirements": [
                    {
                        "description": "销售分析",
                        "acceptance_criteria": ["返回结果"],
                        "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                    }
                ],
                "semantic_request": {"query": "销售"},
            },
            "plan.semantic_request",
            "shape_invalid",
            "已提供",
            "删除 semantic_request",
        ),
        (
            {
                "mode": "discovery",
                "discovery_scope": {
                    "tables": ["dataset"],
                    "columns": ["dataset.value"],
                },
                "requirements": [
                    {
                        "description": "统计值",
                        "acceptance_criteria": ["返回统计值"],
                        "fulfillment": {
                            "mode": "evidence",
                            "assertions": [
                                {
                                    "description": "读取值",
                                    "claim_extractions": [
                                        {
                                            "mode": "scalar",
                                            "name": "值",
                                            "field": "value",
                                            "required": True,
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            },
            "plan.requirements[0].fulfillment.mode",
            "protocol_direction_conflict",
            "evidence",
            "plan.mode 改为 ready",
        ),
    ],
)
async def test_model_runtime_opening_shape_errors_are_field_specific(
    plan, expected_path, expected_reason, expected_actual, expected_action
) -> None:
    chat = RecordingChatModel()
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_opening",
                "name": "start_data_analysis",
                "args": {"plan": plan},
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "请分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请分析当前数据"),
    )

    assert isinstance(result, RunOpeningInvalidFailure)
    issue = result.validation_issues[0]
    assert issue.path == expected_path
    assert issue.repair_reason == expected_reason
    assert issue.actual == expected_actual
    assert issue.expected
    assert issue.rule
    assert expected_action in issue.action
    if expected_action == "删除 semantic_request":
        assert "把 mode 改为 needs_semantic_context" not in issue.action
        assert issue.suggested_mode == "ready"
    assert "call_opening" not in issue.model_dump_json()


def test_opening_validation_issue_occurrences_group_by_required_four_tuple() -> None:
    from pydantic import ValidationError

    error = ValidationError.from_exception_data(
        "AnalysisPlanningDraft",
        [
            {
                "type": "literal_error",
                "loc": ("mode",),
                "msg": "Input should be ready",
                "input": "bad-one",
                "ctx": {"expected": "'ready'"},
            },
            {
                "type": "literal_error",
                "loc": ("mode",),
                "msg": "Input should be ready",
                "input": "bad-two",
                "ctx": {"expected": "'ready'"},
            },
        ],
    )

    issues = model_runtime._opening_validation_issues(error)

    assert len(issues) == 1
    assert issues[0].occurrences == 2
    assert issues[0].path == "mode"


def test_opening_missing_fulfillment_finding_explains_discovery_shape() -> None:
    from pydantic import ValidationError

    try:
        StartDataAnalysisArguments.model_validate(
            {
                "plan": {
                    "mode": "discovery",
                    "requirements": [
                        {
                            "description": "探索趋势",
                            "acceptance_criteria": ["返回观察结果"],
                        }
                    ],
                }
            },
            strict=True,
        )
    except ValidationError as error:
        issues = model_runtime._opening_validation_issues(error)
    else:  # pragma: no cover - defensive assertion for an invalid fixture
        raise AssertionError("expected the missing fulfillment field to fail validation")

    issue = next(item for item in issues if item.path.endswith("requirements[0].fulfillment"))
    assert "Discovery 目标使用 mode=context_only" in issue.action


@pytest.mark.asyncio
async def test_opening_diagnostic_projection_handles_non_mapping_fulfillment() -> None:
    """Malformed nested values must become a repair finding, not crash diagnostics."""

    chat = RecordingChatModel()
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_malformed_fulfillment",
                "name": "start_data_analysis",
                "args": {
                    "plan": {
                        "mode": "ready",
                        "requirements": [
                            {
                                "description": "统计订单",
                                "acceptance_criteria": ["返回订单数"],
                                "fulfillment": "not-an-object",
                            }
                        ],
                    }
                },
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "请分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请分析当前数据"),
    )

    assert isinstance(result, RunOpeningInvalidFailure)
    issue = next(
        item
        for item in result.validation_issues
        if item.path.endswith("requirements[0].fulfillment")
    )
    assert issue.actual
    assert issue.action


@pytest.mark.asyncio
async def test_model_runtime_opening_diagnostic_does_not_echo_untrusted_input() -> None:
    chat = RecordingChatModel()
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_opening",
                "name": "start_data_analysis",
                "args": {
                    "plan": {
                        "mode": "ready",
                        "requirements": [],
                        "unexpected_secret": "password=do-not-echo",
                    }
                },
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "请分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请分析当前数据"),
    )

    assert isinstance(result, RunOpeningInvalidFailure)
    rendered = result.model_dump_json()
    assert "password=do-not-echo" not in rendered
    assert "unexpected_secret" in rendered


@pytest.mark.asyncio
async def test_model_runtime_extra_field_diagnostic_does_not_echo_sensitive_key() -> None:
    chat = RecordingChatModel()
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_extra_secret",
                "name": "start_data_analysis",
                "args": {
                    "plan": {
                        "mode": "ready",
                        "requirements": [],
                        "password=do-not-echo": "secret",
                    }
                },
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "请分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请分析当前数据"),
    )

    assert isinstance(result, RunOpeningInvalidFailure)
    issue = next(item for item in result.validation_issues if item.error_type == "extra_forbidden")
    assert issue.actual == "额外字段"
    assert "password=do-not-echo" not in issue.model_dump_json()
    assert "password=do-not-echo" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_mode",
    [
        "password=do-not-echo",
        "C:\\private\\plan.json",
        "SELECT * FROM users",
    ],
)
async def test_model_runtime_literal_diagnostic_does_not_echo_sensitive_scalar(
    invalid_mode: str,
) -> None:
    chat = RecordingChatModel()
    chat.opening_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_literal_error",
                "name": "start_data_analysis",
                "args": {"plan": {"mode": invalid_mode}},
                "type": "tool_call",
            }
        ],
    )

    result = await _client(chat).open_run(
        "请分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("请分析当前数据"),
    )

    assert isinstance(result, RunOpeningInvalidFailure)
    rendered = result.model_dump_json()
    assert invalid_mode not in rendered
    assert result.validation_issues[0].actual == "枚举值不合法"


@pytest.mark.asyncio
async def test_model_runtime_finalizes_semantic_plan_with_one_terminal_action() -> None:
    chat = RecordingChatModel()
    initial_plan = AnalysisPlanningDraft(
        mode="needs_semantic_context",
        requirements=[
            {
                "description": "说明业务字段",
                "acceptance_criteria": ["依据数据地图或 Schema"],
                "fulfillment": {"mode": "context_only", "sources": ["semantic_context"]},
            }
        ],
        semantic_request={"query": "业务字段"},
    )

    result = await _client(chat).finalize_analysis_plan(
        AnalysisPlanFinalizationRequest(
            question="业务字段是什么意思？",
            physical_schema=_source().schema_summary,
            datasource_revision=1,
            initial_plan=initial_plan,
            semantic_warning=AnalysisWarning(
                code=WarningCode.DATALINK_SCHEMA_ONLY,
                message="DataLink 暂不可用",
            ),
            available_capabilities=["run_sql_readonly", "commit_analysis_claims"],
        ),
        FakeCancellation(),
    )

    assert not isinstance(result, AgentFailure)
    assert result.mode == "ready"
    assert result.semantic_request is None
    assert chat.calls[0] == (
        "bind_tools",
        (["finalize_analysis_plan"], {"strict": True, "parallel_tool_calls": False}),
    )
    prompt = "\n".join(
        message.content
        for name, messages in reversed(chat.calls)
        if name == "ainvoke"
        for message in messages
        if isinstance(message, (SystemMessage, HumanMessage))
    )
    assert "evidence fulfillment 只能有 mode 和 assertions" in prompt
    assert "绝不能直接放在 fulfillment" in prompt
    assert "字段名在 required_schema 的多个来源表中出现" in prompt
    assert "必须使用完整的 table.column（例如 orders.order_id）" in prompt
    assert "constraint.table 只能填写物理表名" in prompt
    assert "不要写 orders.order_id 这类表限定名" not in prompt
    assert "表达式、COUNT DISTINCT、乘法和 CASE 不要放入 aggregate" in prompt
    assert '"fulfillment":{"mode":"evidence","assertions"' in prompt
    assert '"fulfillment":{"mode":"context_only","sources":["schema"]}' in prompt
    assert '"fulfillment":{"mode":"context_only","sources":["schema","semantic_context"]}' in prompt
    assert "结构、关系、连接键使用 context_only" in prompt
    assert "series extraction 必须带 aggregate 或 group_by" in prompt
    assert "结构或关系目标应定稿为 context_only" in prompt
    assert "原问题明确要求 Markdown、表格、图表或报告" in prompt
    assert "不能因为 Discovery 观察不完整就返回 clarification" in prompt
    assert "这是一次执行定稿，不是重新规划" in prompt
    assert "不要再次探索或扩展范围" in prompt


@pytest.mark.asyncio
async def test_model_runtime_finalization_decodes_one_plan_string_layer() -> None:
    chat = RecordingChatModel()
    final_plan = chat.finalization_response.tool_calls[0]["args"]["plan"]
    chat.finalization_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_finalize_encoded",
                "name": "finalize_analysis_plan",
                "args": {"plan": json.dumps(final_plan, ensure_ascii=False)},
                "type": "tool_call",
            }
        ],
    )
    initial_plan = AnalysisPlanningDraft(
        mode="needs_semantic_context",
        requirements=[
            {
                "description": "说明业务字段",
                "acceptance_criteria": ["依据 Schema"],
                "fulfillment": {"mode": "context_only", "sources": ["schema"]},
            }
        ],
        semantic_request={"query": "业务字段"},
    )

    result = await _client(chat).finalize_analysis_plan(
        AnalysisPlanFinalizationRequest(
            question="业务字段是什么意思？",
            physical_schema=_source().schema_summary,
            datasource_revision=1,
            initial_plan=initial_plan,
            semantic_warning=AnalysisWarning(
                code=WarningCode.DATALINK_SCHEMA_ONLY,
                message="DataLink 暂不可用",
            ),
            available_capabilities=["run_sql_readonly"],
        ),
        FakeCancellation(),
    )

    assert isinstance(result, AnalysisPlanningDraft)
    assert result.mode == "ready"


@pytest.mark.asyncio
async def test_model_runtime_finalization_uses_discovery_schema_subset() -> None:
    chat = RecordingChatModel()
    initial_plan = AnalysisPlanningDraft(
        mode="discovery",
        requirements=[
            {
                "description": "探索当前数据",
                "acceptance_criteria": ["形成后续可验证目标"],
                "fulfillment": {"mode": "context_only", "sources": ["schema"]},
            }
        ],
        discovery_scope={
            "tables": ["dataset"],
            "columns": ["dataset.value"],
            "max_rows": 100,
        },
    )
    schema = SchemaSummaryRead(
        datasource_id="datasource_1",
        dialect="duckdb",
        tables=[
            {
                "name": "dataset",
                "columns": [
                    {"name": "value", "type": "INTEGER", "nullable": False},
                    {"name": "internal_note", "type": "TEXT", "nullable": True},
                ],
            },
            {
                "name": "other_table",
                "columns": [{"name": "secret_metric", "type": "INTEGER", "nullable": True}],
            },
        ],
    )

    result = await _client(chat).finalize_analysis_plan(
        AnalysisPlanFinalizationRequest(
            question="先探索一下这份数据",
            physical_schema=schema,
            datasource_revision=1,
            initial_plan=initial_plan,
            discovery_observations=[
                {
                    "tool_call_id": "discovery_1",
                    "audit_log_id": "audit_1",
                    "artifact_id": None,
                    "columns": ["value"],
                    "row_count": 1,
                    "rows_truncated": False,
                }
            ],
            available_capabilities=["run_sql_readonly"],
        ),
        FakeCancellation(),
    )

    assert not isinstance(result, AgentFailure)
    prompt = "\n".join(
        message.content
        for name, messages in chat.calls
        if name == "ainvoke"
        for message in messages
        if isinstance(message, (SystemMessage, HumanMessage))
    )
    assert '"name":"dataset"' in prompt
    assert '"name":"value"' in prompt
    assert "internal_note" not in prompt
    assert "other_table" not in prompt
    assert "secret_metric" not in prompt


_BROKEN_PLAN_ARGS = "BROKEN_PLAN_ARGS_MUST_NOT_LEAK"
_JSON_ERROR_SNIPPET = "SECRET_JSON_SNIPPET"


def _invalid_json_finalization_message() -> AIMessage:
    return AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "finalize_analysis_plan",
                "args": f'{{"plan":{{"mode":"ready",{_BROKEN_PLAN_ARGS}}}}}',
                "id": "call_invalid_json",
                "error": (
                    "JSONDecodeError: Expecting ',' delimiter: "
                    f"line 1 column 2921 (char 2920) {_JSON_ERROR_SNIPPET}"
                ),
                "type": "invalid_tool_call",
            }
        ],
    )


def _discovery_finalization_request() -> AnalysisPlanFinalizationRequest:
    return AnalysisPlanFinalizationRequest(
        question="先探索一下这份数据",
        physical_schema=_source().schema_summary,
        datasource_revision=1,
        initial_plan=AnalysisPlanningDraft(
            mode="discovery",
            requirements=[
                {
                    "description": "探索当前数据",
                    "acceptance_criteria": ["形成后续可验证目标"],
                    "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                }
            ],
            discovery_scope={
                "tables": ["dataset"],
                "columns": ["dataset.value"],
                "max_rows": 100,
            },
        ),
        discovery_observations=[
            {
                "tool_call_id": "discovery_1",
                "audit_log_id": "audit_1",
                "artifact_id": None,
                "columns": ["value"],
                "row_count": 1,
                "rows_truncated": False,
            }
        ],
        available_capabilities=["run_sql_readonly"],
    )


def _ainvoke_count(chat: RecordingChatModel) -> int:
    return sum(1 for name, _messages in chat.calls if name == "ainvoke")


def _finalization_repair_prompt(chat: RecordingChatModel) -> str:
    ainvoke_calls = [messages for name, messages in chat.calls if name == "ainvoke"]
    assert len(ainvoke_calls) >= 2
    return "\n".join(
        message.content
        for message in ainvoke_calls[1]
        if isinstance(message, (SystemMessage, HumanMessage))
    )


@pytest.mark.asyncio
async def test_model_runtime_finalization_repairs_invalid_tool_call_json_once() -> None:
    chat = RecordingChatModel()
    valid_response = chat.finalization_response
    chat.finalization_responses = [_invalid_json_finalization_message(), valid_response]

    result = await _client(chat).finalize_analysis_plan(
        _discovery_finalization_request(),
        FakeCancellation(),
    )

    assert isinstance(result, AnalysisPlanningDraft)
    assert result.mode == "ready"
    assert _ainvoke_count(chat) == 2
    repair_prompt = _finalization_repair_prompt(chat)
    assert "response.invalid_tool_calls" in repair_prompt
    assert "json_invalid" in repair_prompt
    assert "JSON 不合法" in repair_prompt
    assert "重新提交一次" in repair_prompt
    assert "count_invalid" not in repair_prompt
    assert _BROKEN_PLAN_ARGS not in repair_prompt
    assert _JSON_ERROR_SNIPPET not in repair_prompt


@pytest.mark.asyncio
async def test_model_runtime_finalization_rejects_invalid_tool_call_json_after_one_repair() -> None:
    chat = RecordingChatModel()
    chat.finalization_responses = [
        _invalid_json_finalization_message(),
        _invalid_json_finalization_message(),
    ]

    result = await _client(chat).finalize_analysis_plan(
        _discovery_finalization_request(),
        FakeCancellation(),
    )

    assert isinstance(result, AnalysisPlanInvalidFailure)
    assert result.code is AgentErrorCode.ANALYSIS_PLAN_INVALID
    assert _ainvoke_count(chat) == 2
    assert result.validation_issues
    issue = result.validation_issues[0]
    assert issue.path == "response.invalid_tool_calls"
    assert issue.error_type == "json_invalid"
    assert issue.repair_reason == "shape_invalid"
    assert issue.actual == "valid=0 invalid=1 json_decode_error"
    assert "count_invalid" not in issue.error_type
    dumped = result.model_dump_json()
    assert _BROKEN_PLAN_ARGS not in dumped
    assert _JSON_ERROR_SNIPPET not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        AIMessage(content=""),
        AIMessage(content="解释", tool_calls=_analysis_opening_message().tool_calls),
        AIMessage(
            content="",
            tool_calls=[
                *_analysis_opening_message().tool_calls,
                {**_analysis_opening_message().tool_calls[0], "id": "call_opening_2"},
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "unknown",
                    "name": "run_sql_readonly",
                    "args": {"sql": "SELECT 1"},
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    **_analysis_opening_message().tool_calls[0],
                    "args": {"plan": {"mode": "ready", "requirements": []}},
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    **_analysis_opening_message().tool_calls[0],
                    "args": {"plan": {"mode": "discovery", "requirements": []}},
                }
            ],
        ),
    ],
)
async def test_model_runtime_opening_rejects_invalid_shapes(response: AIMessage) -> None:
    chat = RecordingChatModel()
    chat.opening_response = response

    result = await _client(chat).open_run(
        "分析当前数据",
        _source().schema_summary,
        FakeCancellation(),
        opening_context=_opening_context("分析当前数据"),
    )

    assert result.code is AgentErrorCode.MODEL_RUN_OPENING_INVALID


@pytest.mark.asyncio
async def test_model_runtime_stops_before_model_call_when_cancelled_or_deadline_elapsed() -> None:
    canceled_chat = RecordingChatModel()
    canceled = await _client(canceled_chat).invoke_with_tools(
        [HumanMessage(content="分析")],
        [_test_sql_tool],
        FakeCancellation(cancelled=True),
    )
    expired_deadline = RunDeadline(1)
    expired_deadline._deadline = time.monotonic() - 1
    expired_chat = RecordingChatModel()
    expired = await _client(expired_chat, expired_deadline).invoke_with_tools(
        [HumanMessage(content="分析")],
        [_test_sql_tool],
        FakeCancellation(),
    )

    assert canceled.code is AgentErrorCode.RUN_CANCELED
    assert expired.code is AgentErrorCode.ANALYSIS_LIMIT_REACHED
    assert canceled_chat.calls == []
    assert expired_chat.calls == [
        ("bind_tools", (["run_sql_readonly"], {"strict": True, "parallel_tool_calls": False}))
    ]


def test_run_deadline_reserves_preparation_and_final_answer_windows() -> None:
    deadline = RunDeadline(300)

    assert deadline.preparation_remaining_seconds() <= 165
    assert deadline.preparation_stage_remaining_seconds("run_opening") <= 60
    assert deadline.agent_remaining_seconds() <= 255
    assert deadline.agent_turn_remaining_seconds() <= 120
    assert deadline.final_remaining_seconds() <= 300


def test_run_deadline_uses_configured_opening_budget() -> None:
    from application.runtime_limits import RuntimeLimits

    deadline = RunDeadline(
        600,
        limits=RuntimeLimits(
            preparation_max_seconds=240,
            run_opening_timeout_seconds=60,
            semantic_context_timeout_seconds=30,
            analysis_plan_followup_timeout_seconds=240,
        ),
    )

    assert 59 < deadline.preparation_stage_remaining_seconds("run_opening") <= 60


def test_discovery_followup_rebases_on_remaining_run_budget() -> None:
    from application.runtime_limits import RuntimeLimits

    deadline = RunDeadline(
        600,
        limits=RuntimeLimits(
            preparation_max_seconds=300,
            analysis_plan_followup_timeout_seconds=240,
            final_reserve_seconds=45,
        ),
    )
    deadline._preparation_deadline = time.monotonic() + 1

    preparation_window = deadline.preparation_stage_remaining_seconds("analysis_plan_followup")
    discovery_window = deadline.discovery_plan_followup_remaining_seconds()

    assert preparation_window <= 1
    assert 239 < discovery_window <= 240


@pytest.mark.asyncio
async def test_model_runtime_caps_commit_and_final_answer_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部提交和每次最终答案各有独立窗口，不能独占总 Run 时限。"""

    observed_timeouts: list[float] = []

    async def record_timeout(operation, *, cancellation, timeout_seconds):
        del cancellation
        operation.close()
        observed_timeouts.append(timeout_seconds)
        if len(observed_timeouts) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_commit",
                        "name": "commit_analysis_claims",
                        "args": {"claims": []},
                        "type": "tool_call",
                    }
                ],
            )
        return {"markdown": "已生成答案。"}

    monkeypatch.setattr(model_runtime, "await_runtime_call", record_timeout)
    client = _client(RecordingChatModel(), RunDeadline(600))

    commit_result = await client.invoke_with_tools(
        [HumanMessage(content="提交结论")],
        [_test_commit_tool],
        FakeCancellation(),
    )
    final_result = await client.generate_final_answer(
        [HumanMessage(content="生成最终答案")],
        "json_schema",
        FakeCancellation(),
    )

    assert isinstance(commit_result, AIMessage)
    assert final_result == FinalMarkdownPayload(markdown="已生成答案。")
    assert observed_timeouts == [120.0, 45.0]


def test_run_deadline_reserves_cleanup_margin_near_global_deadline() -> None:
    deadline = RunDeadline(600)
    deadline._deadline = time.monotonic() + 45

    remaining = deadline.final_answer_remaining_seconds()

    assert 0 < remaining <= 40


@pytest.mark.asyncio
async def test_model_runtime_distinguishes_stage_timeout_from_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阶段窗口使用独立失败码，真正耗尽总时限才使用全局失败码。"""

    async def timeout_request(operation, *, cancellation, timeout_seconds):
        del cancellation, timeout_seconds
        operation.close()
        raise model_runtime.RuntimeWaitTimedOut

    monkeypatch.setattr(model_runtime, "await_runtime_call", timeout_request)
    client = _client(RecordingChatModel(), RunDeadline(600))

    commit_failure = await client.invoke_with_tools(
        [HumanMessage(content="提交结论")],
        [_test_commit_tool],
        FakeCancellation(),
    )
    final_failure = await client.generate_final_answer(
        [HumanMessage(content="生成最终答案")],
        "json_schema",
        FakeCancellation(),
    )

    assert commit_failure.code is AgentErrorCode.ANALYSIS_CLAIM_COMMIT_TIMEOUT
    assert final_failure.code is AgentErrorCode.FINAL_ANSWER_TIMEOUT


@pytest.mark.asyncio
async def test_model_runtime_caps_each_regular_agent_turn_and_reports_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float] = []

    async def timeout_request(operation, *, cancellation, timeout_seconds):
        del cancellation
        operation.close()
        observed_timeouts.append(timeout_seconds)
        raise model_runtime.RuntimeWaitTimedOut

    monkeypatch.setattr(model_runtime, "await_runtime_call", timeout_request)
    client = _client(RecordingChatModel(), RunDeadline(600))

    failure = await client.invoke_with_tools(
        [HumanMessage(content="继续分析")],
        [_test_sql_tool],
        FakeCancellation(),
    )

    assert failure.code is AgentErrorCode.ANALYSIS_AGENT_TURN_TIMEOUT
    assert observed_timeouts == [120.0]


@pytest.mark.asyncio
async def test_datalink_mcp_port_only_passes_the_allowlisted_explore_arguments() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(endpoint: str, arguments: dict[str, object]) -> object:
        calls.append((endpoint, arguments))
        return {
            "datasource_id": "datasource_1",
            "graph_version": "graph_1",
            "query": "查看订单关系",
            "nodes": [],
            "edges": [],
            "join_paths": [],
            "warnings": [],
        }

    port = DataLinkMcpPort(
        endpoint="http://datalink.test/mcp",
        timeout_seconds=10,
        call_tool=call_tool,
    )
    result = await port.explore(
        DataLinkExploreCommand(
            datasource_id="datasource_1",
            schema_revision=1,
            graph_version="graph_1",
            query="查看订单关系",
            focus="join_paths",
            max_nodes=8,
        ),
        FakeCancellation(),
    )

    assert result.cache_hit is False
    assert calls == [
        (
            "http://datalink.test/mcp",
            {
                "datasource_id": "datasource_1",
                "graph_version": "graph_1",
                "query": "查看订单关系",
                "focus": "join_paths",
                "max_nodes": 8,
            },
        )
    ]


@pytest.mark.asyncio
async def test_datalink_mcp_session_uses_only_read_and_write_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streamable HTTP 的第三项是会话 ID 获取函数，不能传给 ClientSession。"""

    import application.agent_ports as agent_ports

    recorded: dict[str, object] = {}

    class FakeTransport:
        async def __aenter__(self) -> tuple[object, object, object]:
            return ("read-stream", "write-stream", lambda: "session-id")

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_streamable_http_client(endpoint: str) -> FakeTransport:
        recorded["endpoint"] = endpoint
        return FakeTransport()

    def fake_session(*streams: object) -> RecordingMcpSession:
        session = RecordingMcpSession(*streams)
        recorded["session"] = session
        return session

    monkeypatch.setattr(agent_ports, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(agent_ports, "ClientSession", fake_session)
    port = DataLinkMcpPort(endpoint="http://datalink.test/mcp", timeout_seconds=10)

    result = await port._call_datalink_explore(
        "http://datalink.test/mcp",
        {"datasource_id": "datasource_1"},
    )

    session = recorded["session"]
    assert isinstance(session, RecordingMcpSession)
    assert recorded["endpoint"] == "http://datalink.test/mcp"
    assert session.streams == ("read-stream", "write-stream")
    assert session.initialized is True
    assert session.tool_calls == [("datalink_explore", {"datasource_id": "datasource_1"})]
    assert isinstance(result, CallToolResult)


@pytest.mark.asyncio
async def test_datalink_mcp_port_rejects_wrong_fixed_identity_without_downgrade() -> None:
    async def call_tool(_endpoint: str, _arguments: dict[str, object]) -> object:
        return {
            "datasource_id": "other_datasource",
            "graph_version": "graph_1",
            "query": "查看订单关系",
            "nodes": [],
            "edges": [],
            "join_paths": [],
            "warnings": [],
        }

    result = await DataLinkMcpPort(
        endpoint="http://datalink.test/mcp",
        timeout_seconds=10,
        call_tool=call_tool,
    ).explore(
        DataLinkExploreCommand(
            datasource_id="datasource_1",
            schema_revision=1,
            graph_version="graph_1",
            query="查看订单关系",
        ),
        FakeCancellation(),
    )

    assert result.code is AgentErrorCode.DATALINK_REQUEST_INVALID


@pytest.mark.asyncio
async def test_datalink_mcp_port_accepts_the_official_call_result_shape() -> None:
    """官方 MCP 返回用 ``isError``，不能按 Python 风格字段名读取。"""

    payload = {
        "datasource_id": "datasource_1",
        "graph_version": "graph_1",
        "query": "查看订单关系",
        "nodes": [],
        "edges": [],
        "join_paths": [],
        "warnings": [],
    }

    async def call_tool(_endpoint: str, _arguments: dict[str, object]) -> object:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            isError=False,
        )

    result = await DataLinkMcpPort(
        endpoint="http://datalink.test/mcp",
        timeout_seconds=10,
        call_tool=call_tool,
    ).explore(
        DataLinkExploreCommand(
            datasource_id="datasource_1",
            schema_revision=1,
            graph_version="graph_1",
            query="查看订单关系",
        ),
        FakeCancellation(),
    )

    assert result.result.datasource_id == "datasource_1"


@pytest.mark.asyncio
async def test_gateway_agent_port_reuses_service_query_with_run_and_session_references() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=SqlExecutionRead(
            columns=["value"],
            rows=[[1]],
            row_count=1,
            audit_log_id="audit_1",
            artifact_id="artifact_1",
            elapsed_ms=3,
        ),
    )
    port = DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    )
    schema = await port.load_schema(
        SchemaLoadRequest(datasource_id="datasource_1", schema_revision=1),
        FakeCancellation(),
    )
    result = await port.execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT value FROM dataset",
        ),
        FakeCancellation(),
    )

    assert schema.schema_summary.tables[0].name == "dataset"
    assert result.audit_log_id == "audit_1"
    assert result.artifact_id == "artifact_1"
    assert service.sql_calls == [
        {
            "datasource_id": "datasource_1",
            "run_id": "run_1",
            "session_id": "session_1",
            "tool_call_id": "tool_1",
            "sql": "SELECT value FROM dataset",
            "cancel_token": service.sql_calls[0]["cancel_token"],
            "input_snapshot_path": None,
            "repaired_from_audit_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_gateway_agent_port_forwards_discovery_artifact_usage_and_row_cap() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=SqlExecutionRead(
            columns=["value"],
            rows=[[1]],
            row_count=1,
            audit_log_id="audit_discovery",
            artifact_id="artifact_discovery",
            elapsed_ms=3,
        ),
    )

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_discovery",
            sql="SELECT value FROM dataset",
            max_rows=100,
            artifact_usage="discovery_observation",
        ),
        FakeCancellation(),
    )

    assert result.artifact_id == "artifact_discovery"
    assert service.sql_calls[0]["max_rows"] == 100
    assert service.sql_calls[0]["artifact_usage"] == "discovery_observation"


@pytest.mark.asyncio
async def test_gateway_agent_port_passes_run_remaining_sql_budget() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=SqlExecutionRead(
            columns=["value"],
            rows=[[1]],
            row_count=1,
            audit_log_id="audit_1",
            artifact_id="artifact_1",
            elapsed_ms=3,
        ),
    )
    deadline = RunDeadline(600)

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
        deadline=deadline,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT value FROM dataset",
        ),
        FakeCancellation(),
    )

    assert result.audit_log_id == "audit_1"
    assert 0 < service.sql_calls[0]["timeout_seconds"] <= 600


@pytest.mark.asyncio
async def test_gateway_agent_port_preserves_retryable_guard_feedback() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=SqlGuardBlockedError(
            SqlGuardIssue(
                reason_code="UNKNOWN_TABLE",
                subject_kind="table",
                subject="FROM unknown_table",
                location=None,
                message="SQL 引用了不在当前 Schema 中的数据表",
                hint="请只从当前 Schema 中已提供的数据表选择表名。",
                retryable=True,
            ),
            "audit_1",
        ),
    )
    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT * FROM unknown_table",
        ),
        FakeCancellation(),
    )

    assert isinstance(result, SqlExecutionFailure)
    assert result.code is AgentErrorCode.DATA_GATEWAY_BLOCKED
    assert result.reason_code == "UNKNOWN_TABLE"
    assert result.audit_log_id == "audit_1"
    assert result.retryable is True
    assert result.hint == "请只从当前 Schema 中已提供的数据表选择表名。"


@pytest.mark.asyncio
async def test_gateway_agent_port_preserves_query_timeout_audit_for_repair() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=QueryTimeoutError("查询超时", "audit_timeout"),
    )

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT value FROM dataset",
        ),
        FakeCancellation(),
    )

    assert isinstance(result, SqlExecutionFailure)
    assert result.code is AgentErrorCode.DATA_GATEWAY_FAILED
    assert result.reason_code == "QUERY_TIMEOUT"
    assert result.audit_log_id == "audit_timeout"
    assert result.retryable is True


@pytest.mark.asyncio
async def test_gateway_agent_port_keeps_dangerous_guard_block_non_retryable() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=SqlGuardBlockedError(
            SqlGuardIssue(
                reason_code="WRITE_STATEMENT",
                subject_kind="statement",
                subject="DROP",
                location=None,
                message="只允许只读查询",
                hint=None,
                retryable=False,
            ),
            "audit_1",
        ),
    )

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="DROP TABLE dataset",
        ),
        FakeCancellation(),
    )

    assert isinstance(result, SqlExecutionFailure)
    assert result.reason_code == "WRITE_STATEMENT"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_gateway_agent_port_maps_query_failed_error_for_repair() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=QueryFailedError(
            "查询引用了无效的字段或标识符",
            "audit_ident",
            code="QUERY_IDENTIFIER_INVALID",
            hint="请只使用当前 Schema 中已存在的物理列名，不要把 SELECT 别名用于 WHERE 或 HAVING。",
            retryable=True,
        ),
    )

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT value FROM dataset",
        ),
        FakeCancellation(),
    )

    assert isinstance(result, SqlExecutionFailure)
    assert result.code is AgentErrorCode.DATA_GATEWAY_FAILED
    assert result.reason_code == "QUERY_IDENTIFIER_INVALID"
    assert result.subject is None
    assert result.audit_log_id == "audit_ident"
    assert result.retryable is True
    assert result.execution_status == "not_started"
    assert result.hint is not None
    assert "Unknown column" not in result.message
    assert "Unknown column" not in result.hint


@pytest.mark.asyncio
async def test_gateway_agent_port_unclassified_exception_is_not_sql_retryable() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=RuntimeError("pymysql (1045, Access denied for user 'root'@'db.internal')"),
    )

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT value FROM dataset",
        ),
        FakeCancellation(),
    )

    assert isinstance(result, AgentFailure)
    assert result.code is AgentErrorCode.DATA_GATEWAY_FAILED
    assert result.retryable is False
    assert result.message == "SQL 查询失败"


@pytest.mark.asyncio
async def test_gateway_agent_port_connection_query_failed_error_is_not_sql_retryable() -> None:
    service = FakeDataSourceService(
        source=_source(),
        execution=QueryFailedError(
            "数据源连接不可用",
            "audit_conn",
            code="DATASOURCE_CHECK_FAILED",
            retryable=False,
        ),
    )

    result = await DataGatewayAgentPort(
        datasources=service,
        session_id="session_1",
        schema_revision=1,
    ).execute_readonly(
        SqlExecutionRequest(
            datasource_id="datasource_1",
            run_id="run_1",
            tool_call_id="tool_1",
            sql="SELECT value FROM dataset",
        ),
        FakeCancellation(),
    )

    assert isinstance(result, SqlExecutionFailure)
    assert result.code is AgentErrorCode.DATA_GATEWAY_FAILED
    assert result.reason_code == "DATASOURCE_CHECK_FAILED"
    assert result.reason_code != "UNKNOWN_COLUMN"
    assert result.audit_log_id == "audit_conn"
    assert result.retryable is False
    assert result.execution_status == "not_started"
    assert result.subject is None
