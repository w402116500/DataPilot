from __future__ import annotations

import asyncio
import json

import pytest
from agent_runtime.analysis_planning import MaterializedAnalysisPlan
from agent_runtime.contracts import (
    AgentArtifactType,
    AgentErrorCode,
    AgentFailure,
    AgentWorkingSetProjection,
    AnalysisAggregateConstraint,
    AnalysisAssertion,
    AnalysisClaimValue,
    AnalysisClarificationDraft,
    AnalysisExecutionConstraints,
    AnalysisQueryAttempt,
    AnalysisRequirement,
    AnalysisSourceConstraint,
    AnalysisValidationFinding,
    AnalysisVerifiedValue,
    ArtifactRef,
    ConversationContext,
    DataLinkExploreResponse,
    DiscoveryScopeDraft,
    FinalMarkdownPayload,
    RunContext,
    SandboxExecutionResult,
    SandboxExecutionStatus,
    SchemaContext,
    SqlExecutionFailure,
    SqlExecutionResult,
    ToolObservation,
    WarningCode,
)
from agent_runtime.conversation_context import discovery_schema_index
from agent_runtime.graph import (
    _CONTEXT_BUDGET_PARTIAL_ANSWER,
    _SAFE_EVIDENCE_PARTIAL_ANSWER,
    _STAGE_INSTRUCTION_TOKEN_RESERVE,
    GraphRunError,
    _AnalysisCommitArguments,
    _AnalysisScope,
    _bound_tool_content,
    _bound_tool_message,
    _build_agent_working_set,
    _execute_datalink,
    _execute_python,
    _execute_sql,
    _fit_agent_working_set_projection,
    _initial_messages,
    _is_system_metadata_query,
    _messages_for_model,
    _NeverCanceled,
    _PendingSqlRepair,
    _persisted_tool_summary,
    _project_final_answer_fact_values,
    _PythonArguments,
    _record_assertion_failures,
    _record_sql_analysis_progress,
    _redact,
    _sql_fingerprint,
    _SqlAnalysisProgress,
    _SqlArguments,
    _tool_message_for_observation,
    _validate_sql_action,
    run_analysis_graph,
)
from agent_runtime.graph import (
    GraphDependencies as _GraphDependencies,
)
from agent_runtime.runtime_limits import AgentRuntimeLimits
from agent_runtime.token_estimate import estimate_message_tokens, estimate_text_tokens
from contracts.datalink import (
    DataLinkColumnProfileRead,
    DataLinkEdgeEvidenceRead,
    DataLinkEdgeType,
    DataLinkExploreResult,
    DataLinkJoinPathRead,
    DataLinkJoinPathStepRead,
    DataLinkNodeRead,
    DataLinkNodeType,
)
from contracts.datasources import (
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
    TableDataRead,
)
from contracts.run_events import RunEventType
from contracts.runs import ModelRuntimeSnapshot
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def test_repeated_contract_failure_blocks_only_the_affected_assertion() -> None:
    observation = ToolObservation(
        tool_call_id="call_contract",
        tool_name="run_sql_readonly",
        status="failed",
        summary={
            "error_code": AgentErrorCode.ANALYSIS_SQL_CONTRACT_INVALID.value,
            "reason_code": AgentErrorCode.ANALYSIS_SQL_CONTRACT_INVALID.value,
            "validation_findings": [
                {
                    "code": "SQL_CONTRACT_FILTER_MISSING:customer_id",
                    "message": "缺少客户编号空值过滤。",
                    "severity": "error",
                    "assertion_id": "R1.A1",
                }
            ],
        },
    )
    counts: dict[str, int] = {}
    blocked: set[str] = set()

    assert _record_assertion_failures(observation, counts, blocked) == []
    assert _record_assertion_failures(observation, counts, blocked) == ["R1.A1"]
    assert blocked == {"R1.A1"}

    findings = _validate_sql_action(
        ["R1"],
        ["R1.A1", "R1.A2"],
        [
            AnalysisRequirement(
                id="R1",
                description="检查数据质量",
                acceptance_criteria=["返回可核验结果"],
            )
        ],
        [],
        blocked_assertion_ids=blocked,
    )

    assert any(code == "ANALYSIS_ASSERTION_BLOCKED:R1.A1" for code, _ in findings)
    assert all("R1.A2" not in code for code, _ in findings)


def test_query_attempt_limit_is_enforced_per_assertion() -> None:
    attempts = [
        AnalysisQueryAttempt(
            id=f"Q{index}",
            requirement_ids=["R1"],
            assertion_ids=["R1.A1"],
        )
        for index in range(1, 4)
    ]

    findings = _validate_sql_action(
        ["R1"],
        ["R1.A1", "R1.A2"],
        [
            AnalysisRequirement(
                id="R1",
                description="检查数据质量",
                acceptance_criteria=["返回可核验结果"],
            )
        ],
        attempts,
        max_query_attempts_per_assertion=3,
    )

    assert any(code == "ANALYSIS_ASSERTION_QUERY_LIMIT_REACHED:R1.A1" for code, _ in findings)
    assert all("R1.A2" not in code for code, _ in findings)


def test_messages_for_model_keeps_complete_tool_batches_and_drops_orphans() -> None:
    """Working-set trimming never sends a provider-invalid half tool turn."""

    messages: list[BaseMessage] = [
        SystemMessage(content="contract"),
        HumanMessage(content="question"),
    ]
    for index in range(12):
        call_id = f"call_{index}"
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": call_id,
                            "name": "run_sql_readonly",
                            "args": {"sql": f"SELECT {index}"},
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content=f"observation_{index}", tool_call_id=call_id),
            ]
        )
    messages.extend(
        [
            ToolMessage(content="orphan", tool_call_id="missing_call"),
            HumanMessage(content="latest question"),
        ]
    )

    working_set = _messages_for_model(messages)

    assert working_set[:2] == messages[:2]
    assert len(working_set) <= 24
    assert all(
        not isinstance(message, ToolMessage)
        or (
            index > 0
            and isinstance(working_set[index - 1], AIMessage)
            and message.tool_call_id
            in {
                call.get("id")
                for call in working_set[index - 1].tool_calls
                if isinstance(call, dict)
            }
        )
        for index, message in enumerate(working_set)
    )
    assert "orphan" not in {str(message.content) for message in working_set}
    for index, message in enumerate(working_set):
        if isinstance(message, AIMessage) and message.tool_calls:
            assert index + 1 < len(working_set)
            assert isinstance(working_set[index + 1], ToolMessage)
            assert working_set[index + 1].tool_call_id == message.tool_calls[0]["id"]


def _token_budget_snapshot(input_budget_tokens: int) -> ModelRuntimeSnapshot:
    return ModelRuntimeSnapshot(
        profile_id="profile_graph",
        provider="openai-compatible",
        model_name="demo-model",
        base_url="https://example.com/v1",
        temperature=0,
        run_timeout_seconds=120,
        final_output_mode="markdown",
        model_capability_fingerprint="f" * 64,
        context_window_tokens=max(32_768, input_budget_tokens),
        input_budget_tokens=input_budget_tokens,
    )


def _wide_working_set_limits() -> AgentRuntimeLimits:
    return AgentRuntimeLimits(max_model_context_chars=120_000, max_model_messages=32)


def _tool_pair(
    index: int,
    *,
    name: str = "run_sql_readonly",
    content: str | None = None,
) -> list[BaseMessage]:
    call_id = f"call_{index}"
    args = {"query": f"item {index}"} if name == "explore_datalink" else {"sql": f"SELECT {index}"}
    observation = content or json.dumps(
        {"status": "succeeded", "index": index},
        ensure_ascii=False,
    )
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "name": name,
                    "args": args,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=observation, tool_call_id=call_id, name=name),
    ]


def test_token_estimator_treats_cjk_as_one_token() -> None:
    assert estimate_text_tokens("abcd") == 1.0
    assert estimate_text_tokens("汉字") == 2.0
    assert estimate_text_tokens("汉A") == 1.25


def test_working_set_keeps_latest_datalink_pair_under_chinese_head_token_budget() -> None:
    """~18k mixed chars must not be compared to input_budget_tokens as a char cap."""

    human = ("问" * 5_000) + ("A" * 13_000)
    datalink_payload = {
        "status": "succeeded",
        "tool_name": "explore_datalink",
        "summary": {
            "semantic_context": {
                "mode": "live",
                "graph_version": "graph_1",
                "fields": [
                    {
                        "table": "customers",
                        "column": "channel",
                        "semantic_type": "category",
                        "provenance": "semantic_mapping",
                        "aliases": [],
                        "semantic_mappings": [
                            {
                                "concept": "获客渠道",
                                "concept_provenance": "semantic_mapping",
                                "provenance": "semantic_mapping",
                                "field_to_concept_confidence": 0.91,
                                "entities": [
                                    {
                                        "name": "客户",
                                        "entity_provenance": "semantic_mapping",
                                        "provenance": "semantic_mapping",
                                        "entity_to_concept_confidence": 0.8,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "relationships": [],
                "join_paths": [],
            }
        },
    }
    messages: list[BaseMessage] = [
        SystemMessage(content="contract"),
        HumanMessage(content=human),
        *_tool_pair(
            1,
            name="explore_datalink",
            content=json.dumps(datalink_payload, ensure_ascii=False),
        ),
        ToolMessage(content="orphan", tool_call_id="missing_call"),
    ]

    build = _build_agent_working_set(
        {"messages": messages},
        context=_context(),
        scope=_AnalysisScope(),
        model_context=_token_budget_snapshot(15_360),
        stage="agent",
        limits=_wide_working_set_limits(),
    )

    tool_ids = [
        message.tool_call_id for message in build.messages if isinstance(message, ToolMessage)
    ]
    assert "call_1" in tool_ids
    assert "missing_call" not in tool_ids
    explore = next(
        message
        for message in build.messages
        if isinstance(message, ToolMessage) and message.tool_call_id == "call_1"
    )
    payload = json.loads(str(explore.content))
    semantic = payload["summary"]["semantic_context"]
    assert semantic["fields"][0]["table"] == "customers"
    assert semantic["fields"][0]["column"] == "channel"
    assert "获客渠道" in json.dumps(semantic, ensure_ascii=False)
    assert "semantic_mapping" in json.dumps(semantic, ensure_ascii=False)


def test_working_set_compacted_ignores_char_count_versus_token_budget() -> None:
    human = ("问" * 5_000) + ("A" * 13_000)
    assert len(human) > 15_360
    build = _build_agent_working_set(
        {"messages": [SystemMessage(content="contract"), HumanMessage(content=human)]},
        context=_context(),
        scope=_AnalysisScope(),
        model_context=_token_budget_snapshot(15_360),
        stage="agent",
        limits=_wide_working_set_limits(),
    )

    assert build.compacted is False
    assert estimate_message_tokens(build.messages) <= (15_360 - _STAGE_INSTRUCTION_TOKEN_RESERVE)


def test_working_set_keeps_newest_complete_pairs_that_fit_token_budget() -> None:
    messages: list[BaseMessage] = [
        SystemMessage(content="contract"),
        HumanMessage(content="question"),
    ]
    for index in range(3):
        messages.extend(_tool_pair(index))
    limits = _wide_working_set_limits()

    all_kept = _messages_for_model(messages, max_context_tokens=10_000, limits=limits)
    assert [message.tool_call_id for message in all_kept if isinstance(message, ToolMessage)] == [
        "call_0",
        "call_1",
        "call_2",
    ]

    newest_two = [
        *messages[:2],
        *messages[4:6],
        *messages[6:8],
    ]
    two_pair_budget = estimate_message_tokens(newest_two) + 8
    assert estimate_message_tokens(messages) > two_pair_budget
    kept_two = _messages_for_model(messages, max_context_tokens=two_pair_budget, limits=limits)
    assert [message.tool_call_id for message in kept_two if isinstance(message, ToolMessage)] == [
        "call_1",
        "call_2",
    ]


def test_working_set_truncates_newest_tool_message_instead_of_dropping_the_pair() -> None:
    huge = json.dumps({"status": "succeeded", "blob": "汉" * 20_000}, ensure_ascii=False)
    messages: list[BaseMessage] = [
        SystemMessage(content="contract"),
        HumanMessage(content="question"),
        *_tool_pair(9, content=huge),
    ]
    head_and_call = estimate_message_tokens(messages[:3])
    kept = _messages_for_model(
        messages,
        max_context_tokens=head_and_call + 240,
        limits=_wide_working_set_limits(),
    )

    assert len(kept) == 4
    assert isinstance(kept[2], AIMessage)
    assert isinstance(kept[3], ToolMessage)
    assert kept[3].tool_call_id == "call_9"
    parsed = json.loads(str(kept[3].content))
    assert parsed["status"] == "succeeded"


def test_working_set_fit_reserves_stage_instruction_tokens() -> None:
    human = ("问" * 5_000) + ("A" * 13_000)
    snapshot = _token_budget_snapshot(15_360)
    build = _build_agent_working_set(
        {"messages": [SystemMessage(content="contract"), HumanMessage(content=human)]},
        context=_context(),
        scope=_AnalysisScope(),
        model_context=snapshot,
        stage="agent",
        limits=_wide_working_set_limits(),
    )

    assert estimate_message_tokens(build.messages) <= (
        snapshot.input_budget_tokens - _STAGE_INSTRUCTION_TOKEN_RESERVE
    )


def test_persisted_python_observation_drops_stdout_and_stderr() -> None:
    """Sandbox 输出可能包含原始数据，只能留在进程内观察中。"""

    observation = ToolObservation(
        tool_call_id="call_python",
        tool_name="run_python",
        status="failed",
        summary={
            "error_code": "SANDBOX_FAILED",
            "retryable": False,
            "sandbox_status": "failed",
            "elapsed_ms": 12,
            "exit_code": 1,
            "output_count": 0,
            "stdout": "敏感原始数据",
            "stderr": "绝对路径 C:/private/input.csv",
        },
    )

    persisted = _persisted_tool_summary(observation)

    assert "stdout" not in persisted
    assert "stderr" not in persisted
    assert persisted["error_code"] == "SANDBOX_FAILED"


def test_bound_tool_content_keeps_oversized_json_parseable() -> None:
    content = json.dumps(
        {
            "status": "succeeded",
            "verified_values": [{"value": index} for index in range(500)],
        },
        ensure_ascii=False,
    )

    bounded = _bound_tool_content(content, 800)

    assert len(bounded) <= 800
    assert json.loads(bounded)["status"] == "succeeded"


def test_bound_tool_content_removes_single_oversized_nested_item() -> None:
    content = json.dumps(
        {
            "status": "succeeded",
            "summary": {"verified_values": [{"value": "x" * 5_000}]},
        },
        ensure_ascii=False,
    )

    bounded = _bound_tool_content(content, 200)

    assert len(bounded) <= 200
    assert json.loads(bounded)["status"] == "succeeded"


def test_working_set_compaction_drops_single_oversized_value() -> None:
    projection = AgentWorkingSetProjection(
        question="当前问题",
        current_requirement_ids=["R1"],
        verified_values=[
            {
                "name": "total",
                "value": "x" * 10_000,
                "assertion_id": "R1.A1",
                "fact_key": "total",
                "dimensions": {},
            }
        ],
        incomplete_goals=["确认结果"],
    )

    fitted, encoded = _fit_agent_working_set_projection(projection, 4_000)

    assert len(encoded) <= 4_000
    assert fitted.verified_values == []


def test_working_set_compaction_fails_when_fixed_fields_cannot_fit() -> None:
    projection = AgentWorkingSetProjection(
        question="x" * 10_000,
        current_requirement_ids=["R1"],
    )

    with pytest.raises(RuntimeError, match="Working Set"):
        _fit_agent_working_set_projection(projection, 4_000)


@pytest.mark.asyncio
async def test_graph_closes_agent_turn_when_working_set_cannot_fit() -> None:
    """不可压缩的 Working Set 必须收口当前回合，不能留下悬挂 started。"""

    model = FakeModel([], [])
    events = RecordingEvents()
    context = _context().model_copy(update={"question": "测" * 8_000})
    model_context = ModelRuntimeSnapshot(
        profile_id="profile_graph",
        provider="openai-compatible",
        model_name="demo-model",
        base_url="https://example.com/v1",
        temperature=0,
        run_timeout_seconds=120,
        final_output_mode="markdown",
        model_capability_fingerprint="f" * 64,
        context_window_tokens=8_000,
        input_budget_tokens=8_000,
    )

    with pytest.raises(GraphRunError) as error:
        await run_analysis_graph(
            context,
            GraphDependencies(
                model=model,
                gateway=FakeGateway(),
                events=events,
                plan=_materialized_plan("R1"),
                model_context=model_context,
            ),
        )

    assert error.value.failure.code is AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED
    assert model.agent_messages == []
    turn_events = [
        event
        for event in events.events
        if event.type in {RunEventType.AGENT_TURN_STARTED, RunEventType.AGENT_TURN_COMPLETED}
    ]
    assert [event.type for event in turn_events] == [
        RunEventType.AGENT_TURN_STARTED,
        RunEventType.AGENT_TURN_COMPLETED,
    ]
    assert turn_events[1].payload["failure_code"] == AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED.value


class FakeModel:
    def __init__(
        self,
        agent_responses: list[AIMessage | AgentFailure],
        final_responses: list[FinalMarkdownPayload | AgentFailure],
    ) -> None:
        self.agent_responses = agent_responses
        self.final_responses = final_responses
        self.bound_tool_names: list[list[str]] = []
        self.agent_messages: list[list[BaseMessage]] = []
        self.final_messages: list[list[BaseMessage]] = []
        self.final_modes: list[str] = []

    async def invoke_with_tools(self, messages, tools, _cancellation):
        self.agent_messages.append(list(messages))
        self.bound_tool_names.append([item.name for item in tools])
        if not self.agent_responses:
            return AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="test model responses exhausted",
            )
        return self.agent_responses.pop(0)

    async def generate_final_answer(self, messages, mode, _cancellation):
        self.final_messages.append(list(messages))
        self.final_modes.append(mode)
        return self.final_responses.pop(0)

    async def generate_final_answer_stream(self, messages, _cancellation):
        self.final_messages.append(list(messages))
        self.final_modes.append("markdown")
        answer = self.final_responses.pop(0)
        if isinstance(answer, AgentFailure):
            return answer

        async def chunks():
            yield answer.markdown

        return chunks()


class ChunkedFakeModel(FakeModel):
    async def generate_final_answer_stream(self, messages, _cancellation):
        self.final_messages.append(list(messages))
        self.final_modes.append("markdown")
        answer = self.final_responses.pop(0)
        if isinstance(answer, AgentFailure):
            return answer

        async def chunks():
            for index in range(0, len(answer.markdown), 200):
                yield answer.markdown[index : index + 200]

        return chunks()


def _materialized_plan(*requirement_ids: str) -> MaterializedAnalysisPlan:
    """Graph tests start from the same server-owned plan as production."""

    requirements = tuple(
        AnalysisRequirement(
            id=requirement_id,
            description=f"确认第 {index} 项结果",
            acceptance_criteria=["结果可核验"],
            assertions=[
                AnalysisAssertion(
                    id=f"{requirement_id}.A1",
                    requirement_id=requirement_id,
                    description=f"读取 {requirement_id} 的结果",
                    source_tables=["sales"],
                    claim_extractions=[
                        {
                            "mode": "scalar",
                            "name": "total",
                            "field": "total",
                            "required": True,
                        }
                    ],
                )
            ],
        )
        for index, requirement_id in enumerate(requirement_ids, start=1)
    )
    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=requirements,
        constraints=AnalysisExecutionConstraints(),
    )


def _answer_only_plan() -> MaterializedAnalysisPlan:
    """Final Answer tests need a non-context-only plan without data requirements."""

    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=(),
        constraints=AnalysisExecutionConstraints(),
    )


def _context_only_plan() -> MaterializedAnalysisPlan:
    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="根据当前安全上下文回答",
                acceptance_criteria=["回答只使用当前 Run 上下文"],
                fulfillment_mode="context_only",
                context_sources=["schema"],
                status="context_ready",
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
    )


def _context_only_semantic_plan() -> MaterializedAnalysisPlan:
    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="说明当前数据之间的关系",
                acceptance_criteria=["依据 Schema 和语义图谱回答"],
                fulfillment_mode="context_only",
                context_sources=["schema", "semantic_context"],
                status="context_ready",
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
    )


def _mixed_non_evidence_plan() -> MaterializedAnalysisPlan:
    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="根据当前安全上下文回答",
                acceptance_criteria=["回答只使用当前 Run 上下文"],
                fulfillment_mode="context_only",
                context_sources=["schema"],
                status="context_ready",
            ),
            AnalysisRequirement(
                id="R2",
                description="导出当前数据报告",
                acceptance_criteria=["登记报告产物"],
                fulfillment_mode="blocked",
                block_reason="capability_unavailable",
                status="blocked",
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
    )


def _discovery_plan() -> MaterializedAnalysisPlan:
    return MaterializedAnalysisPlan(
        mode="discovery",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="探索可能的分布模式",
                acceptance_criteria=["形成后续可验证目标"],
                fulfillment_mode="context_only",
                context_sources=["schema"],
                status="context_ready",
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
        discovery_scope=DiscoveryScopeDraft(
            tables=["sales"],
            columns=["amount"],
            max_rows=100,
        ),
    )


def _plan_with_assertion(assertion: AnalysisAssertion) -> MaterializedAnalysisPlan:
    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description=assertion.description,
                acceptance_criteria=["结果可核验"],
                assertions=[assertion],
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
    )


def GraphDependencies(*args, plan=None, **kwargs):
    """Keep test call sites focused on Graph behavior, not construction noise."""

    if plan is None:
        model = kwargs.get("model")
        plan = getattr(model, "plan", None) or (_materialized_plan("R1"))
    return _GraphDependencies(*args, plan=plan, **kwargs)


class FakeGateway:
    def __init__(
        self,
        results: list[SqlExecutionResult | SqlExecutionFailure | AgentFailure] | None = None,
        schema_summary: SchemaSummaryRead | None = None,
    ) -> None:
        self.results = results or []
        self.schema_summary = schema_summary
        self.sql_requests = []

    async def load_schema(self, request, _cancellation):
        return SchemaContext(
            datasource_id=request.datasource_id,
            schema_revision=request.schema_revision,
            schema_summary=self.schema_summary
            or SchemaSummaryRead(
                datasource_id=request.datasource_id,
                dialect="duckdb",
                tables=[
                    SchemaTableRead(
                        name="sales",
                        columns=[SchemaColumnRead(name="amount", type="INTEGER", nullable=False)],
                    )
                ],
            ),
        )

    async def execute_readonly(self, request, _cancellation):
        self.sql_requests.append(request)
        return self.results.pop(0)


class FakeDataLink:
    def __init__(
        self, result: DataLinkExploreResult | DataLinkExploreResponse | AgentFailure
    ) -> None:
        self.result = result
        self.requests = []

    async def explore(self, request, _cancellation):
        self.requests.append(request)
        if isinstance(self.result, AgentFailure):
            return self.result
        if isinstance(self.result, DataLinkExploreResponse):
            return self.result
        return DataLinkExploreResponse(result=self.result, cache_hit=False)


class RecordingEvents:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        return None


def _context() -> RunContext:
    return RunContext(
        run_id="run_graph",
        session_id="session_graph",
        datasource_id="datasource_graph",
        model_profile_id="profile_graph",
        model_name="demo-model",
        schema_revision=1,
        input_snapshot_ref="snapshots/run_graph/source.csv",
        input_filename="source.csv",
        question="查看数据结构",
    )


@pytest.mark.asyncio
async def test_discovery_executes_one_governed_sql_without_claims() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_sql_1",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": (
                                "WITH selected AS (SELECT amount FROM sales) "
                                "SELECT amount FROM selected ORDER BY amount"
                            )
                        },
                        "type": "tool_call",
                    }
                ],
            )
        ],
        [],
    )
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["amount"], rows=[[10]], row_count=1),
                audit_log_id="audit_discovery_1",
                artifact_id="artifact_discovery_1",
                elapsed_ms=4,
            )
        ]
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            events=events,
            plan=_discovery_plan(),
        ),
    )

    assert len(gateway.sql_requests) == 1
    assert gateway.sql_requests[0].max_rows == 100
    assert gateway.sql_requests[0].artifact_usage == "discovery_observation"
    assert gateway.sql_requests[0].requirement_ids == []
    assert gateway.sql_requests[0].assertion_ids == []
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "DISCOVERY_OBSERVATION_ONLY"
    assert state.outcome.evidence_refs == []
    assert state.outcome.claim_audits == []
    assert model.bound_tool_names == [["run_sql_readonly"]]
    succeeded = next(event for event in events.events if event.type is RunEventType.TOOL_SUCCEEDED)
    assert succeeded.payload["evidence_count"] == 0
    assert "discovery_observation" in succeeded.payload["output_summary_json"]
    observed = next(
        event for event in events.events if event.type is RunEventType.ANALYSIS_DISCOVERY_OBSERVED
    )
    assert observed.payload == {
        "tool_call_id": "discovery_sql_1",
        "tool_name": "run_sql_readonly",
        "turn_no": 1,
        "audit_log_id": "audit_discovery_1",
        "artifact_id": "artifact_discovery_1",
        "column_count": 1,
        "row_count": 1,
        "rows_truncated": False,
    }
    assert "sql" not in observed.payload


@pytest.mark.asyncio
async def test_discovery_sql_repair_ignores_text_and_runs_unique_new_query() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_bad",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 5"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="这次改成更简单的聚合。",
                tool_calls=[
                    {
                        "id": "discovery_repaired",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 10"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway(
        [
            SqlExecutionFailure(
                code=AgentErrorCode.DATA_GATEWAY_FAILED,
                reason_code="QUERY_GROUP_BY_INVALID",
                subject_kind="statement",
                message="查询的 GROUP BY 或聚合不合法",
                hint="请将所有非聚合列写入 GROUP BY，或改为聚合计算。",
                audit_log_id="audit_discovery_bad",
                retryable=True,
            ),
            SqlExecutionResult(
                result=TableDataRead(columns=["amount"], rows=[[10]], row_count=1),
                audit_log_id="audit_discovery_repaired",
                artifact_id="artifact_discovery_repaired",
                elapsed_ms=2,
            ),
        ]
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            events=events,
            plan=_discovery_plan(),
        ),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == [
        "discovery_bad",
        "discovery_repaired",
    ]
    assert gateway.sql_requests[1].repaired_from_audit_id == "audit_discovery_bad"
    assert state.outcome.incomplete_reason == "DISCOVERY_OBSERVATION_ONLY"
    assert state.outcome.answer != "Discovery SQL 修复返回了无效响应。"
    assert [item.tool_call_id for item in state.discovery_observations] == ["discovery_repaired"]
    repair_prompt = str(model.agent_messages[1][0].content)
    assert "安全网关拒绝" not in repair_prompt
    assert "已通过安全检查但执行失败" in repair_prompt
    completed = [
        event for event in events.events if event.type is RunEventType.AGENT_TURN_COMPLETED
    ]
    repair_completed = next(event for event in completed if event.payload["turn_no"] == 2)
    assert repair_completed.payload["status"] == "completed"
    assert repair_completed.payload["tool_call_count"] == 1
    assert repair_completed.payload["tool_names"] == "run_sql_readonly"


@pytest.mark.asyncio
async def test_discovery_bare_retryable_agent_failure_does_not_enter_sql_repair() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_sql_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 5"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "should_not_run",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 10"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway(
        [
            AgentFailure(
                code=AgentErrorCode.DATA_GATEWAY_FAILED,
                message="SQL 查询失败",
                retryable=True,
            )
        ]
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            events=RecordingEvents(),
            plan=_discovery_plan(),
        ),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["discovery_sql_1"]
    assert len(model.agent_messages) == 1
    assert state.outcome.incomplete_reason == "DISCOVERY_QUERY_FAILED"
    assert state.outcome.answer == "Discovery 查询失败，暂未形成可验证结论。"
    assert state.discovery_observations == []


@pytest.mark.asyncio
async def test_discovery_non_retryable_sql_failure_does_not_enter_sql_repair() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_sql_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 5"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "should_not_run",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 10"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway(
        [
            SqlExecutionFailure(
                code=AgentErrorCode.DATA_GATEWAY_FAILED,
                reason_code="DATASOURCE_CHECK_FAILED",
                subject_kind="statement",
                message="数据源连接不可用",
                audit_log_id="audit_conn",
                retryable=False,
            )
        ]
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            events=RecordingEvents(),
            plan=_discovery_plan(),
        ),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["discovery_sql_1"]
    assert len(model.agent_messages) == 1
    assert state.outcome.incomplete_reason == "DISCOVERY_QUERY_FAILED"
    assert state.outcome.answer == "Discovery 查询时数据源连接不可用，暂未形成可验证结论。"
    assert "安全检查" not in state.outcome.answer
    assert state.discovery_observations == []


@pytest.mark.asyncio
async def test_discovery_retries_one_retryable_sql_failure_with_new_query() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_bad",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 5"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_repaired",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales LIMIT 10"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway(
        [
            SqlExecutionFailure(
                code=AgentErrorCode.DATA_GATEWAY_BLOCKED,
                reason_code="UNKNOWN_COLUMN",
                subject_kind="column",
                subject="missing",
                message="SQL 引用了不在当前 Schema 中的字段",
                hint="请只使用当前 Schema 中已提供的字段。",
                audit_log_id="audit_discovery_bad",
                retryable=True,
            ),
            SqlExecutionResult(
                result=TableDataRead(columns=["amount"], rows=[[10]], row_count=1),
                audit_log_id="audit_discovery_repaired",
                artifact_id="artifact_discovery_repaired",
                elapsed_ms=2,
            ),
        ]
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            events=RecordingEvents(),
            plan=_discovery_plan(),
        ),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == [
        "discovery_bad",
        "discovery_repaired",
    ]
    assert gateway.sql_requests[1].repaired_from_audit_id == "audit_discovery_bad"
    assert state.outcome.incomplete_reason == "DISCOVERY_OBSERVATION_ONLY"
    assert [item.tool_call_id for item in state.discovery_observations] == ["discovery_repaired"]
    assert model.bound_tool_names == [["run_sql_readonly"], ["run_sql_readonly"]]
    repair_feedback = [message for message in model.agent_messages[1] if message.type == "tool"]
    assert len(repair_feedback) == 1
    assert "UNKNOWN_COLUMN" in str(repair_feedback[0].content)
    assert "安全网关拒绝" in str(model.agent_messages[1][0].content)
    assert "安全错误" not in str(model.agent_messages[0][0].content)


@pytest.mark.asyncio
async def test_discovery_model_receives_only_allowlisted_schema_subset() -> None:
    """完整 Schema 不应诱导 Discovery 模型引用白名单外字段。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_subset_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales"},
                        "type": "tool_call",
                    }
                ],
            )
        ],
        [],
    )
    schema = SchemaSummaryRead(
        datasource_id="datasource_graph",
        dialect="duckdb",
        tables=[
            SchemaTableRead(
                name="sales",
                columns=[
                    SchemaColumnRead(name="amount", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="internal_note", type="TEXT", nullable=True),
                ],
                primary_key=["amount"],
            ),
            SchemaTableRead(
                name="customers",
                columns=[SchemaColumnRead(name="customer_id", type="INTEGER", nullable=False)],
            ),
        ],
    )
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["amount"], rows=[[10]], row_count=1),
                audit_log_id="audit_discovery_subset",
                artifact_id="artifact_discovery_subset",
                elapsed_ms=1,
            )
        ],
        schema_summary=schema,
    )

    await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, plan=_discovery_plan()),
    )

    human_message = model.agent_messages[0][1].content
    assert '"name": "sales"' in human_message
    assert '"name": "amount"' in human_message
    assert "internal_note" not in human_message
    assert "customers" not in human_message


def test_discovery_schema_index_filters_relationship_metadata_with_scope() -> None:
    schema = SchemaSummaryRead(
        datasource_id="datasource_graph",
        dialect="duckdb",
        tables=[
            SchemaTableRead(
                name="orders",
                columns=[
                    SchemaColumnRead(name="order_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="customer_id", type="INTEGER", nullable=False),
                ],
                primary_key=["order_id"],
                foreign_keys=[
                    {
                        "columns": ["customer_id"],
                        "referenced_table": "customers",
                        "referenced_columns": ["customer_id"],
                    }
                ],
            ),
            SchemaTableRead(
                name="customers",
                columns=[SchemaColumnRead(name="customer_id", type="INTEGER", nullable=False)],
            ),
        ],
    )

    projection = discovery_schema_index(
        schema,
        DiscoveryScopeDraft(tables=["orders"], columns=["orders.order_id"], max_rows=100),
    )

    assert [table.name for table in projection.tables] == ["orders"]
    assert [column.name for column in projection.tables[0].columns] == ["order_id"]
    assert projection.tables[0].primary_key == ["order_id"]
    assert projection.tables[0].foreign_keys == []


@pytest.mark.asyncio
async def test_discovery_rejects_mixed_text_and_sql_action_before_gateway() -> None:
    """Discovery must not execute a query while silently discarding model text."""

    model = FakeModel(
        [
            AIMessage(
                content="我已经发现一个异常趋势。",
                tool_calls=[
                    {
                        "id": "discovery_mixed",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales"},
                        "type": "tool_call",
                    }
                ],
            )
        ],
        [],
    )
    gateway = FakeGateway()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            plan=_discovery_plan(),
            limits=AgentRuntimeLimits(max_discovery_attempts=1),
        ),
    )

    assert gateway.sql_requests == []
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "DISCOVERY_PLAN_INVALID"
    assert state.outcome.evidence_refs == []
    assert state.outcome.claim_audits == []


@pytest.mark.asyncio
async def test_discovery_repairs_mixed_text_and_sql_once_before_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="我先说明一下。",
                tool_calls=[
                    {
                        "id": "discovery_mixed_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_mixed_2",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales ORDER BY amount"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["amount"], rows=[[10]], row_count=1),
                audit_log_id="audit_discovery_repaired",
                artifact_id="artifact_discovery_repaired",
                elapsed_ms=4,
            )
        ]
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, events=events, plan=_discovery_plan()),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["discovery_mixed_2"]
    assert state.outcome.incomplete_reason == "DISCOVERY_OBSERVATION_ONLY"
    completed = [
        event for event in events.events if event.type is RunEventType.AGENT_TURN_COMPLETED
    ]
    assert [event.payload["turn_no"] for event in completed] == [1, 2]
    assert completed[0].payload["failure_code"] == AgentErrorCode.MODEL_OUTPUT_INVALID.value


@pytest.mark.asyncio
async def test_discovery_format_repair_failure_does_not_execute_sql() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="说明文字",
                tool_calls=[
                    {
                        "id": "discovery_mixed_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="仍然解释",
                tool_calls=[
                    {
                        "id": "discovery_mixed_2",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway()
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, events=events, plan=_discovery_plan()),
    )

    assert gateway.sql_requests == []
    assert state.outcome.incomplete_reason == "DISCOVERY_PLAN_INVALID"
    completed = [
        event for event in events.events if event.type is RunEventType.AGENT_TURN_COMPLETED
    ]
    assert [event.payload["turn_no"] for event in completed] == [1, 2]


@pytest.mark.asyncio
async def test_discovery_repairs_multiple_sql_calls_before_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_sql_1",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales"},
                        "type": "tool_call",
                    },
                    {
                        "id": "discovery_sql_2",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT COUNT(*) FROM sales"},
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "discovery_sql_repaired",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount FROM sales ORDER BY amount"},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [],
    )
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["amount"], rows=[[10]], row_count=1),
                audit_log_id="audit_discovery_repaired",
                artifact_id="artifact_discovery_repaired",
                elapsed_ms=4,
            )
        ]
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, events=events, plan=_discovery_plan()),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["discovery_sql_repaired"]
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "DISCOVERY_OBSERVATION_ONLY"
    assert state.outcome.evidence_refs == []
    assert state.outcome.claim_audits == []
    repair_feedback = [message for message in model.agent_messages[1] if message.type == "tool"]
    assert [message.tool_call_id for message in repair_feedback] == [
        "discovery_sql_1",
        "discovery_sql_2",
    ]
    assert all(
        "DISCOVERY_TOOL_CALL_COUNT_INVALID" in str(message.content) for message in repair_feedback
    )
    completed = [
        event for event in events.events if event.type is RunEventType.AGENT_TURN_COMPLETED
    ]
    assert [event.payload["turn_no"] for event in completed] == [1, 2]


def _semantic_datalink_result() -> DataLinkExploreResult:
    """构造带敏感画像的完整回包，验证 Runtime 不会把画像交给模型。"""

    return DataLinkExploreResult(
        datasource_id="datasource_graph",
        graph_version="graph_1",
        query="查看客户订单关联",
        nodes=[
            DataLinkNodeRead(id="table:orders", type=DataLinkNodeType.TABLE, name="orders"),
            DataLinkNodeRead(
                id="column:orders:customer_id",
                type=DataLinkNodeType.COLUMN,
                name="customer_id",
                table="orders",
                description="订单所属客户",
                profile=DataLinkColumnProfileRead(
                    column_id="column:orders:customer_id",
                    dtype="INTEGER",
                    null_rate=0,
                    distinct_count=10,
                    unique_rate=0.1,
                    sample_values=["private-customer-value"],
                    top_values=["private-top-value"],
                ),
            ),
            DataLinkNodeRead(
                id="column:customers:id",
                type=DataLinkNodeType.COLUMN,
                name="id",
                table="customers",
                description="客户主键",
            ),
            DataLinkNodeRead(
                id="concept:customer_identifier",
                type=DataLinkNodeType.CONCEPT,
                name="客户标识",
                description="用于唯一标识下单客户的业务编号。",
                aliases=["客户 ID"],
            ),
            DataLinkNodeRead(
                id="entity:customer",
                type=DataLinkNodeType.ENTITY,
                name="客户",
                description="在系统中下单的个人或企业客户。",
                aliases=["买家"],
            ),
        ],
        edges=[
            {
                "source": "column:orders:customer_id",
                "target": "concept:customer_identifier",
                "type": DataLinkEdgeType.REPRESENTS,
                "confidence": 0.9,
            },
            {
                "source": "entity:customer",
                "target": "concept:customer_identifier",
                "type": DataLinkEdgeType.HAS_CONCEPT,
                "confidence": 0.9,
            },
            {
                "source": "column:orders:customer_id",
                "target": "column:customers:id",
                "type": DataLinkEdgeType.FOREIGN_KEY,
                "confidence": 1.0,
                "evidence": DataLinkEdgeEvidenceRead(
                    kind="explicit_foreign_key",
                    summary="orders.customer_id references customers.id",
                ),
            },
        ],
        join_paths=[
            DataLinkJoinPathRead(
                tables=["orders", "customers"],
                steps=[
                    DataLinkJoinPathStepRead(
                        source_table="orders",
                        source_column="customer_id",
                        target_table="customers",
                        target_column="id",
                        edge_type=DataLinkEdgeType.FOREIGN_KEY,
                        confidence=1.0,
                    )
                ],
                confidence=1.0,
            )
        ],
        warnings=["当前版本只返回有限的语义子图。"],
    )


def _answer(text: str, evidence_refs: list[str] | None = None) -> FinalMarkdownPayload:
    del evidence_refs
    return FinalMarkdownPayload(markdown=text)


def _final_answer_snapshot(model: FakeModel) -> dict[str, object]:
    human = next(message for message in model.final_messages[0] if message.type == "human")
    payload = json.loads(str(human.content))
    assert isinstance(payload, dict)
    return payload


def _explore_datalink_call(tool_call_id: str = "call_explore_relations") -> dict[str, object]:
    return {
        "id": tool_call_id,
        "name": "explore_datalink",
        "args": {"query": "表之间的关系", "max_nodes": 12},
        "type": "tool_call",
    }


def _tool_call(tool_call_id: str, sql: str) -> dict[str, object]:
    return {
        "id": tool_call_id,
        "name": "run_sql_readonly",
        "args": {
            "sql": sql,
            "requirement_ids": ["R1"],
            "assertion_ids": ["R1.A1"],
        },
        "type": "tool_call",
    }


def _commit_call(
    tool_call_id: str = "commit_r1",
    *,
    requirement_id: str = "R1",
    evidence_binding_ids: list[str] | None = None,
    claim: str = "目标结论已由当前 Run 的证据确认。",
    values: list[dict[str, object]] | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "name": "commit_analysis_claims",
                "args": {
                    "claims": [
                        {
                            "requirement_id": requirement_id,
                            "claim": claim,
                            "evidence_binding_ids": (
                                ["E1"] if evidence_binding_ids is None else evidence_binding_ids
                            ),
                            "values": (
                                values
                                if values is not None
                                else [{"name": "total", "value": 42, "unit": None}]
                            ),
                        }
                    ]
                },
                "type": "tool_call",
            }
        ],
    )


def _sql_result(number: int) -> SqlExecutionResult:
    return SqlExecutionResult(
        result=TableDataRead(columns=["total"], rows=[[number]], row_count=1),
        audit_log_id=f"audit_{number}",
        artifact_id=f"table_{number}",
        elapsed_ms=1,
    )


def _series_sql_result() -> SqlExecutionResult:
    return SqlExecutionResult(
        result=TableDataRead(
            columns=["order_month", "order_count"],
            rows=[["2026-01", 3], ["2026-02", 5]],
            row_count=2,
        ),
        audit_log_id="audit_series",
        artifact_id="table_series",
        elapsed_ms=1,
    )


def _sql_guard_failure(
    *,
    audit_log_id: str,
    reason_code: str = "SQL_PARSE_ERROR",
    subject: str = "AVG(not.fully.paid)",
) -> SqlExecutionFailure:
    return SqlExecutionFailure(
        code=AgentErrorCode.DATA_GATEWAY_BLOCKED,
        reason_code=reason_code,
        subject_kind="sql_fragment",
        subject=subject,
        location={"line": 1, "column": 15},
        message="SQL 无法解析",
        hint="当前 Schema 中的字段 not.fully.paid 含特殊字符，必须逐字写为双引号字段名。",
        audit_log_id=audit_log_id,
        retryable=True,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT column_name FROM information_schema.columns",
        "SELECT * FROM sqlite_master",
        "PRAGMA table_info('sales')",
        "SHOW TABLES",
        "DESCRIBE sales",
        "SELECT * FROM duckdb_columns()",
    ],
)
def test_system_metadata_query_detection(sql: str) -> None:
    assert _is_system_metadata_query(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount FROM sales",
        "SELECT * FROM information_schema.columns; SELECT * FROM sales",
    ],
)
def test_metadata_recovery_does_not_classify_business_or_multi_statement_sql(sql: str) -> None:
    assert _is_system_metadata_query(sql) is False


def test_sql_analysis_progress_creates_one_query_and_one_binding_per_requirement() -> None:
    assertions = [
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="统计订单量",
            claim_extractions=[
                {"mode": "scalar", "name": "order_count", "field": "order_count", "required": True}
            ],
        ),
        AnalysisAssertion(
            id="R2.A1",
            requirement_id="R2",
            description="比较订单量",
            claim_extractions=[
                {"mode": "scalar", "name": "order_count", "field": "order_count", "required": True}
            ],
        ),
    ]
    requirements = [
        AnalysisRequirement(
            id="R1",
            description="统计订单量",
            acceptance_criteria=["结果可追溯"],
            assertions=[assertions[0]],
        ),
        AnalysisRequirement(
            id="R2",
            description="比较订单量",
            acceptance_criteria=["结果可追溯"],
            assertions=[assertions[1]],
        ),
    ]
    progress = _SqlAnalysisProgress(
        requirement_ids=["R1", "R2"],
        assertion_ids=["R1.A1", "R2.A1"],
        assertions=assertions,
        sql="SELECT city, COUNT(*) AS order_count FROM orders GROUP BY city",
        expected_columns=["city", "order_count"],
        artifact_id="artifact_1",
        audit_log_id="audit_1",
        result_fields=["city", "order_count"],
        result_validation_findings=[],
        verified_values=[
            AnalysisVerifiedValue(
                name="order_count",
                value=12,
                assertion_id="R1.A1",
            )
        ],
        valid=True,
    )

    next_requirements, attempts, bindings = _record_sql_analysis_progress(
        requirements,
        [],
        [],
        progress,
    )

    assert attempts[0].id == "Q1"
    assert attempts[0].status == "evidenced"
    assert attempts[0].verified_values == progress.verified_values
    assert [
        (binding.id, binding.requirement_id, binding.query_attempt_id) for binding in bindings
    ] == [
        ("E1", "R1", "Q1"),
        ("E2", "R2", "Q1"),
    ]
    assert [requirement.status for requirement in next_requirements] == ["evidenced", "evidenced"]
    assert next_requirements[0].evidence_binding_ids == ["E1"]
    assert next_requirements[1].evidence_binding_ids == ["E2"]

    failed_progress = progress.__class__(
        **{
            **progress.__dict__,
            "result_validation_findings": [
                AnalysisValidationFinding(
                    code="RESULT_CHECK_NON_EMPTY_FAILED",
                    message="结果为空",
                    severity="error",
                    assertion_id="R1.A1",
                )
            ],
            "verified_values": [],
            "valid": False,
        }
    )
    final_requirements, failed_attempts, final_bindings = _record_sql_analysis_progress(
        next_requirements,
        attempts,
        bindings,
        failed_progress,
    )

    assert failed_attempts[1].id == "Q2"
    assert failed_attempts[1].status == "executed"
    assert final_bindings == bindings
    assert [requirement.status for requirement in final_requirements] == ["evidenced", "evidenced"]

    planned_progress = progress.__class__(
        **{
            **progress.__dict__,
            "artifact_id": None,
            "audit_log_id": None,
            "result_fields": [],
            "verified_values": [],
            "valid": False,
            "query_status": "planned",
            "validation_findings": [
                AnalysisValidationFinding(
                    code="SQL_CONTRACT_AGGREGATE_MISMATCH:count",
                    message="正式契约要求 COUNT(*) AS total_orders。",
                    severity="error",
                    assertion_id="R1.A1",
                )
            ],
        }
    )
    planned_requirements, planned_attempts, planned_bindings = _record_sql_analysis_progress(
        requirements,
        [],
        [],
        planned_progress,
    )

    assert planned_attempts[0].status == "planned"
    assert planned_attempts[0].valid is False
    assert planned_attempts[0].audit_log_id is None
    assert planned_attempts[0].artifact_id is None
    assert (
        planned_attempts[0].validation_findings[0].code == "SQL_CONTRACT_AGGREGATE_MISMATCH:count"
    )
    assert [requirement.status for requirement in planned_requirements] == ["pending", "pending"]
    assert planned_bindings == []


def test_redact_preserves_business_values_but_keeps_runtime_protection() -> None:
    text = (
        "日期 2023-01-01，手机号 13800138000，邮箱 buyer@example.com，"
        "api_key=sk-live，路径 C:\\private\\orders.csv"
    )

    redacted = _redact(text)

    assert "2023-01-01" in redacted
    assert "13800138000" in redacted
    assert "buyer@example.com" in redacted
    assert "[secret]" in redacted
    assert "[path]" in redacted
    assert "[phone]" not in redacted
    assert "[email]" not in redacted


@pytest.mark.asyncio
async def test_final_answer_and_draft_preserve_unmasked_business_values() -> None:
    answer = "截至 2023-01-01，手机号 13800138000，邮箱 buyer@example.com。"
    model = FakeModel([AIMessage(content="直接回答")], [_answer(answer, ["schema"])])
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            events=events,
            plan=_answer_only_plan(),
        ),
    )

    draft = "".join(
        event.payload["delta"] for event in events.events if event.type.value == "answer.delta"
    )
    assert state.outcome.answer == answer
    assert draft == answer


@pytest.mark.asyncio
async def test_streaming_final_answer_emits_multiple_deltas_before_completion() -> None:
    answer = "结论\n" + ("已验证内容。" * 80)
    model = ChunkedFakeModel([AIMessage(content="直接回答")], [_answer(answer, ["schema"])])
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            events=events,
            plan=_answer_only_plan(),
        ),
    )

    deltas = [
        event.payload["delta"] for event in events.events if event.type is RunEventType.ANSWER_DELTA
    ]
    assert state.outcome.answer == answer
    assert len(deltas) > 1
    assert "".join(deltas) == answer


@pytest.mark.asyncio
async def test_schema_question_enters_final_answer_without_data_tool() -> None:
    model = FakeModel([], [_answer("当前有 sales 表，字段包括 amount。")])
    gateway = FakeGateway()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, plan=_context_only_plan()),
    )

    assert state.outcome.answer == "当前有 sales 表，字段包括 amount。"
    assert "根据当前数据源的结构信息" not in state.outcome.answer
    assert gateway.sql_requests == []
    assert model.agent_messages == []
    assert len(model.final_messages) == 1
    assert state.outcome.completion_kind == "completed"
    assert state.outcome.evidence_refs == []


@pytest.mark.asyncio
async def test_mixed_context_and_blocked_plan_does_not_start_toolless_agent_loop() -> None:
    model = FakeModel([], [])
    gateway = FakeGateway()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, plan=_mixed_non_evidence_plan()),
    )

    assert model.agent_messages == []
    assert model.final_messages == []
    assert gateway.sql_requests == []
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_REQUIREMENTS_BLOCKED"
    assert "受当前数据、能力或用户约束限制" in state.outcome.answer


@pytest.mark.asyncio
async def test_evidence_plan_with_sql_forbidden_fails_before_agent_loop() -> None:
    plan = _materialized_plan("R1")
    plan = plan.__class__(
        mode=plan.mode,
        requirements=plan.requirements,
        constraints=AnalysisExecutionConstraints(forbidden_tools=["run_sql_readonly"]),
    )
    model = FakeModel([], [])

    with pytest.raises(GraphRunError) as exc_info:
        await run_analysis_graph(
            _context(),
            GraphDependencies(model=model, gateway=FakeGateway(), plan=plan),
        )

    assert exc_info.value.failure.code is AgentErrorCode.ANALYSIS_PLAN_INVALID
    assert model.agent_messages == []
    assert exc_info.value.failure.validation_issues[0].error_type == "capability_conflict"


@pytest.mark.asyncio
async def test_agent_turn_persists_model_reasoning_and_assistant_output() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="我会先核对数据范围。",
                additional_kwargs={"reasoning_content": "先确认指标定义，再读取受控摘要。"},
            )
        ],
        [_answer("有 sales 表。", ["schema"])],
    )
    events = RecordingEvents()

    await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway(), events=events)
    )

    completed = next(
        event for event in events.events if event.type is RunEventType.AGENT_TURN_COMPLETED
    )
    assert completed.payload["reasoning"] == "先确认指标定义，再读取受控摘要。"
    assert completed.payload["assistant_output"] == "我会先核对数据范围。"


@pytest.mark.asyncio
async def test_stage_unavailable_sql_returns_structured_observation_without_gateway_retry() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("unavailable_sql", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(),
        ],
        [_answer("金额为 42。", ["audit_42"])],
    )
    events = RecordingEvents()
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_1"]
    unavailable_messages = [
        item
        for item in model.agent_messages[2]
        if item.type == "tool" and item.tool_call_id == "unavailable_sql"
    ]
    assert len(unavailable_messages) == 1
    observation = json.loads(unavailable_messages[0].content)
    assert observation["status"] == "failed"
    assert observation["summary"]["execution_status"] == "not_started"
    assert observation["summary"]["current_phase"] == "evidence_commit"
    assert observation["summary"]["requested_tool"] == "run_sql_readonly"
    assert observation["summary"]["allowed_actions"] == ["commit_analysis_claims"]
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_stage_unavailable_python_does_not_enter_sandbox() -> None:
    class RecordingSandbox:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise AssertionError("阶段外 Python 不应进入沙箱")

    sandbox = RecordingSandbox()
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "unavailable_python",
                        "name": "run_python",
                        "args": {
                            "script": "print('should not run')",
                            "output_paths": ["charts/result.png"],
                            "purpose": "阶段外测试",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(),
        ],
        [_answer("金额为 42。", ["audit_42"])],
    )
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            workspace_id="workspace_1",
            workspaces=object(),
            sandbox=sandbox,
            artifacts=object(),
        ),
    )

    assert sandbox.calls == 0
    unavailable_messages = [
        item
        for item in model.agent_messages[2]
        if item.type == "tool" and item.tool_call_id == "unavailable_python"
    ]
    observation = json.loads(unavailable_messages[0].content)
    assert observation["summary"]["requested_tool"] == "run_python"
    assert observation["summary"]["execution_status"] == "not_started"
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_python_requires_reported_evidence_before_entering_sandbox() -> None:
    class RecordingSandbox:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("未提交证据时不应进入沙箱")

    chart_context = _context().model_copy(update={"question": "根据已核验结果生成两张柱状图"})
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "python_before_query",
                        "name": "run_python",
                        "args": {
                            "script": "print('should not run')",
                            "output_paths": ["charts/result.png"],
                            "purpose": "生成图表",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_chart", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "python_before_commit",
                        "name": "run_python",
                        "args": {
                            "script": "print('should not run')",
                            "output_paths": ["charts/result.png"],
                            "purpose": "生成图表",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "python_after_commit",
                        "name": "run_python",
                        "args": {
                            "script": "print('chart')",
                            "output_paths": ["charts/result.png"],
                            "purpose": "生成图表",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [_answer("已生成图表", ["query_chart"])],
    )
    sandbox = RecordingSandbox()
    model.plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=_materialized_plan("R1").requirements,
        constraints=AnalysisExecutionConstraints(
            required_artifacts=[
                {"kind": "chart", "minimum_count": 2, "description": "生成两张柱状图"}
            ]
        ),
    )

    class SuccessfulWorkspaces:
        async def write_analysis_script(self, _workspace_id: str, _script: str) -> None:
            return None

    class SuccessfulSandbox:
        def __init__(self, recorder: RecordingSandbox) -> None:
            self.recorder = recorder

        async def execute(self, *_args, **_kwargs):
            self.recorder.calls += 1
            return SandboxExecutionResult(
                status=SandboxExecutionStatus.COMPLETED,
                elapsed_ms=1,
                exit_code=0,
            )

    class SuccessfulArtifacts:
        def __init__(self) -> None:
            self.requests = []

        async def register_file(self, request, _cancellation):
            self.requests.append(request)
            return ArtifactRef(
                artifact_id="chart_result",
                type=AgentArtifactType.CHART,
                title=request.title,
                source_tool_call_id=request.source_tool_call_id,
            )

    artifact_writer = SuccessfulArtifacts()
    state = await run_analysis_graph(
        chart_context,
        GraphDependencies(
            model=model,
            gateway=FakeGateway([_sql_result(42)]),
            workspace_id="workspace_1",
            workspaces=SuccessfulWorkspaces(),
            sandbox=SuccessfulSandbox(sandbox),
            artifacts=artifact_writer,
        ),
    )

    assert sandbox.calls == 1
    assert artifact_writer.requests[0].source_tool_call_id == "python_after_commit"
    blocked = [
        item
        for item in model.agent_messages[1]
        if item.type == "tool" and item.tool_call_id == "python_before_query"
    ][0]
    assert json.loads(blocked.content)["summary"]["execution_status"] == "not_started"
    assert [artifact.artifact_id for artifact in state.artifact_refs] == [
        "table_42",
        "chart_result",
    ]
    chart_turns = [
        index
        for index, tool_names in enumerate(model.bound_tool_names)
        if tool_names == ["run_python"]
    ]
    assert chart_turns
    chart_prompt = model.agent_messages[chart_turns[0]][0].content
    assert "当前阶段：正式产物交付" in chart_prompt
    assert "本回合允许工具：run_python" in chart_prompt
    assert "不要调用 generate_chart、create_chart、plot_chart" in chart_prompt
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_required_markdown_artifact_is_generated_after_reported_evidence() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_report", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write_report",
                        "name": "run_python",
                        "args": {
                            "script": (
                                "from pathlib import Path\nPath('report.md').write_text('# 报告')"
                            ),
                            "output_paths": ["report.md"],
                            "purpose": "生成 Markdown 报告",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [_answer("报告已生成。", ["query_report"])],
    )
    model.plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=_materialized_plan("R1").requirements,
        constraints=AnalysisExecutionConstraints(
            required_artifacts=[
                {"kind": "markdown", "minimum_count": 1, "description": "客户分析报告"}
            ]
        ),
    )

    class SuccessfulWorkspaces:
        async def write_analysis_script(self, _workspace_id: str, _script: str) -> None:
            return None

    class SuccessfulSandbox:
        async def execute(self, *_args, **_kwargs):
            return SandboxExecutionResult(
                status=SandboxExecutionStatus.COMPLETED,
                elapsed_ms=1,
                exit_code=0,
            )

    class SuccessfulArtifacts:
        async def register_file(self, request, _cancellation):
            return ArtifactRef(
                artifact_id="markdown_report",
                type=AgentArtifactType.MARKDOWN,
                title=request.title,
                source_tool_call_id=request.source_tool_call_id,
            )

    state = await run_analysis_graph(
        _context().model_copy(update={"question": "生成一份 Markdown 报告"}),
        GraphDependencies(
            model=model,
            gateway=FakeGateway([_sql_result(42)]),
            workspace_id="workspace_1",
            workspaces=SuccessfulWorkspaces(),
            sandbox=SuccessfulSandbox(),
            artifacts=SuccessfulArtifacts(),
        ),
    )

    delivery_turn = next(
        index
        for index, tool_names in enumerate(model.bound_tool_names)
        if tool_names == ["run_python"]
    )
    delivery_prompt = model.agent_messages[delivery_turn][0].content
    assert "当前阶段：正式产物交付" in delivery_prompt
    assert "Markdown 报告 1 个" in delivery_prompt
    assert "report.md" in delivery_prompt
    assert [artifact.type for artifact in state.artifact_refs] == [
        AgentArtifactType.TABLE,
        AgentArtifactType.MARKDOWN,
    ]
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_missing_required_markdown_artifact_finishes_partial() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_report", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(),
            AIMessage(content="报告正文已在聊天中提供。"),
            AIMessage(content="不再生成文件。"),
        ],
        [_answer("已完成分析，但未生成正式报告文件。", ["query_report"])],
    )
    model.plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=_materialized_plan("R1").requirements,
        constraints=AnalysisExecutionConstraints(
            required_artifacts=[
                {"kind": "markdown", "minimum_count": 1, "description": "客户分析报告"}
            ]
        ),
    )

    state = await run_analysis_graph(
        _context().model_copy(update={"question": "生成一份 Markdown 报告"}),
        GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)])),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ARTIFACT_DELIVERABLE_REQUIRED"
    assert all(artifact.type is not AgentArtifactType.MARKDOWN for artifact in state.artifact_refs)


@pytest.mark.asyncio
async def test_non_retryable_sandbox_delivery_failure_finishes_with_specific_reason() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_report", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write_report",
                        "name": "run_python",
                        "args": {
                            "script": (
                                "from pathlib import Path\nPath('report.md').write_text('# 报告')"
                            ),
                            "output_paths": ["report.md"],
                            "purpose": "生成 Markdown 报告",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [_answer("数据结论已核验，但 Sandbox 暂时不可用。", ["query_report"])],
    )
    model.plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=_materialized_plan("R1").requirements,
        constraints=AnalysisExecutionConstraints(
            required_artifacts=[
                {"kind": "markdown", "minimum_count": 1, "description": "客户分析报告"}
            ]
        ),
    )

    class SuccessfulWorkspaces:
        async def write_analysis_script(self, _workspace_id: str, _script: str) -> None:
            return None

    class UnavailableSandbox:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return SandboxExecutionResult(
                status=SandboxExecutionStatus.FAILED,
                elapsed_ms=3,
                failure=AgentFailure(
                    code=AgentErrorCode.SANDBOX_FAILED,
                    message="Sandbox 运行环境不可用",
                    retryable=False,
                ),
            )

    sandbox = UnavailableSandbox()
    events = RecordingEvents()
    state = await run_analysis_graph(
        _context().model_copy(update={"question": "生成一份 Markdown 报告"}),
        GraphDependencies(
            model=model,
            gateway=FakeGateway([_sql_result(42)]),
            workspace_id="workspace_1",
            workspaces=SuccessfulWorkspaces(),
            sandbox=sandbox,
            artifacts=object(),
            events=events,
        ),
    )

    assert sandbox.calls == 1
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == AgentErrorCode.SANDBOX_FAILED.value
    failed_event = next(event for event in events.events if event.type is RunEventType.TOOL_FAILED)
    assert failed_event.payload["error_message"] == "Sandbox 运行环境不可用"
    assert failed_event.payload["retryable"] is False


@pytest.mark.asyncio
async def test_repeated_retryable_sandbox_failures_are_not_masked_by_turn_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr("agent_runtime.graph._MAX_TURNS", 4)
    python_calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"write_report_{index}",
                    "name": "run_python",
                    "args": {
                        "script": "raise RuntimeError('retry')",
                        "output_paths": ["report.md"],
                        "purpose": "生成 Markdown 报告",
                    },
                    "type": "tool_call",
                }
            ],
        )
        for index in range(2)
    ]
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_report", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(),
            *python_calls,
        ],
        [_answer("数据结论已核验，但报告文件生成失败。", ["query_report"])],
    )
    model.plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=_materialized_plan("R1").requirements,
        constraints=AnalysisExecutionConstraints(
            required_artifacts=[
                {"kind": "markdown", "minimum_count": 1, "description": "客户分析报告"}
            ]
        ),
    )

    class SuccessfulWorkspaces:
        async def write_analysis_script(self, _workspace_id: str, _script: str) -> None:
            return None

    class RetryableSandboxFailure:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return SandboxExecutionResult(
                status=SandboxExecutionStatus.FAILED,
                elapsed_ms=3,
                failure=AgentFailure(
                    code=AgentErrorCode.SANDBOX_FAILED,
                    message="分析脚本执行失败",
                    retryable=True,
                ),
            )

    sandbox = RetryableSandboxFailure()
    state = await run_analysis_graph(
        _context().model_copy(update={"question": "生成一份 Markdown 报告"}),
        GraphDependencies(
            model=model,
            gateway=FakeGateway([_sql_result(42)]),
            workspace_id="workspace_1",
            workspaces=SuccessfulWorkspaces(),
            sandbox=sandbox,
            artifacts=object(),
        ),
    )

    assert sandbox.calls == 2
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == AgentErrorCode.SANDBOX_FAILED.value


@pytest.mark.asyncio
async def test_same_turn_stage_unavailable_tools_get_one_recovery_turn() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("unavailable_sql_1", "SELECT amount AS total FROM sales"),
                    _tool_call("unavailable_sql_2", "SELECT amount AS total FROM sales"),
                ],
            ),
            _commit_call(),
        ],
        [_answer("金额为 42。", ["audit_42"])],
    )
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_1"]
    unavailable_messages = [
        item
        for item in model.agent_messages[2]
        if item.type == "tool" and item.tool_call_id in {"unavailable_sql_1", "unavailable_sql_2"}
    ]
    assert [item.tool_call_id for item in unavailable_messages] == [
        "unavailable_sql_1",
        "unavailable_sql_2",
    ]
    assert all("execution_status" in item.content for item in unavailable_messages)
    assert model.bound_tool_names[2] == ["commit_analysis_claims"]
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_repeated_stage_unavailable_tools_finish_as_partial_after_retry_limit() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("unavailable_sql_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "unavailable_python_2",
                        "name": "run_python",
                        "args": {
                            "script": "print('should not run')",
                            "output_paths": ["charts/result.png"],
                            "purpose": "连续失败测试",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [_answer("已有查询结果，但本次分析未完成证据提交。", ["audit_42"])],
    )
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_1"]
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "MODEL_TOOL_STAGE_RETRY_EXHAUSTED"
    assert len(model.final_messages) == 1
    second_tool_message = [
        item
        for item in model.agent_messages[2]
        if item.type == "tool" and item.tool_call_id == "unavailable_sql_1"
    ]
    assert "execution_status" in second_tool_message[0].content


@pytest.mark.asyncio
async def test_unregistered_tool_returns_recovery_observation_and_can_retry() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "unknown_tool_call",
                        "name": "run_unknown_tool",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(),
        ],
        [_answer("金额为 42。", ["audit_42"])],
    )
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_1"]
    recovery_messages = [
        item
        for item in model.agent_messages[2]
        if item.type == "tool" and item.tool_call_id == "unknown_tool_call"
    ]
    assert len(recovery_messages) == 1
    assert recovery_messages[0].name == "unknown_tool"
    observation = json.loads(recovery_messages[0].content)
    assert observation["tool_name"] == "unknown_tool"
    assert observation["summary"]["requested_tool"] == "run_unknown_tool"
    assert observation["summary"]["execution_status"] == "not_started"
    assert observation["summary"]["retryable"] is True
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_repeated_unregistered_tools_finish_as_partial_after_retry_limit() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "unknown_tool_1",
                        "name": "generate_chart",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "unknown_tool_2",
                        "name": "plot_chart",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        [_answer("已有查询结果，但工具调用未恢复。", ["audit_42"])],
    )
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_1"]
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "MODEL_TOOL_STAGE_RETRY_EXHAUSTED"


@pytest.mark.asyncio
async def test_materialized_plan_enters_agent_and_final_answer_is_isolated() -> None:
    model = FakeModel([AIMessage(content="字段已足够")], [_answer("有 sales 表。", ["schema"])])
    context = _context()

    await run_analysis_graph(
        context,
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            plan=_answer_only_plan(),
        ),
    )

    initial_system = model.agent_messages[0][0].content
    initial_human = model.agent_messages[0][1].content
    final_messages = model.final_messages[0]
    final_instruction = final_messages[-1].content
    assert len(final_messages) == 2
    assert "本次问题类型" not in initial_system
    assert "quick" not in initial_system
    assert "当前用户目标与服务端校验后的分析契约" in initial_human
    assert "未提取到额外业务目标" in initial_human
    assert "检查结果数量或数量级" not in initial_human
    assert "订单状态分析" not in final_instruction
    assert "工具观察里的 evidence_refs" not in final_instruction
    assert "未提交查询" in final_messages[0].content
    assert "audit_" not in final_instruction
    assert "tool_" not in final_instruction
    assert "call_sql" not in final_instruction
    assert "原始查询结果" not in final_instruction
    assert "当前阶段：常规分析" in initial_system
    assert "本回合允许工具：无" in initial_system
    assert "不得自行创造工具名" in initial_system
    assert "本次 Run 还剩 8 次数据工具额度" in initial_system
    assert "必须分批执行" in initial_system
    assert "用户明确说不需要查询数据时必须遵守" in initial_system
    assert "含点号、空格或其他符号的物理字段名" in initial_system
    assert "charts/*.png" in initial_system
    assert "逐字一致" in initial_system
    assert "Noto Sans CJK JP" in initial_system
    assert "不要把 matplotlib 的 font.sans-serif 设置为 DejaVu Sans" in initial_system
    assert "提交 claim 时必须把当前查询结果中与用户目标直接相关的具体数字" in initial_system
    assert "只有当该目标的所有 required assertions 都已有 verified_values" in initial_system
    assert "正式分析契约中的每个 sql_constraints 都是硬约束" in initial_system
    assert "不要把 CASE、表达式、子查询或 CTE 派生值伪装成单列 aggregate" in initial_system
    assert "purpose 等分组维度" not in initial_system


@pytest.mark.asyncio
async def test_clarification_plan_returns_before_agent_loop() -> None:
    model = FakeModel([AIMessage(content="不应进入主 Agent")], [])
    plan = MaterializedAnalysisPlan(
        mode="clarification",
        requirements=(),
        constraints=AnalysisExecutionConstraints(),
        clarification=AnalysisClarificationDraft(
            question="你想先看数据总量、字段质量，还是某个业务指标？",
            missing_items=["data_scope"],
        ),
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context().model_copy(update={"question": "整体看一下数据情况"}),
        GraphDependencies(model=model, gateway=FakeGateway(), events=events, plan=plan),
    )

    assert state.outcome.completion_kind == "clarification"
    assert state.outcome.answer == "你想先看数据总量、字段质量，还是某个业务指标？"
    assert model.agent_messages == []
    event_types = [event.type for event in events.events]
    assert RunEventType.ANALYSIS_CLARIFICATION_REQUESTED in event_types
    assert RunEventType.ANSWER_DELTA in event_types


def test_python_tool_schema_describes_allowed_output_paths() -> None:
    schema = _PythonArguments.model_json_schema()
    output_schema = schema["properties"]["output_paths"]

    assert "charts/*.png" in output_schema["description"]
    assert "逐字一致" in output_schema["description"]
    assert output_schema["examples"] == [["charts/result.png"]]


def test_sql_tool_schema_describes_analysis_bindings() -> None:
    schema = _SqlArguments.model_json_schema()

    assert "含点号、空格或其他符号" in schema["properties"]["sql"]["description"]
    assert "requirement_ids" in schema["properties"]["requirement_ids"]["description"]
    assert "assertion_ids" in schema["properties"]["assertion_ids"]["description"]
    assert "expected_columns" not in schema["properties"]


def test_initial_messages_project_only_the_current_run_formal_contract() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="CURRENT_CONTRACT_ONLY",
        source_tables=["sales"],
        dimensions=["amount"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="sales"),
            AnalysisAggregateConstraint(
                kind="aggregate", function="sum", column="amount", alias="total"
            ),
        ],
        result_checks=[{"kind": "not_null", "required": True, "fields": ["amount", "total"]}],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "总额",
                "field": "total",
                "selector": {"amount": 42},
                "required": True,
            }
        ],
    )
    scope = _AnalysisScope(
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="CURRENT_REQUIREMENT_ONLY",
                acceptance_criteria=["结果可核验"],
                assertions=[assertion],
            ),
        )
    )
    schema_context = SchemaContext(
        datasource_id="datasource_graph",
        schema_revision=1,
        schema_summary=SchemaSummaryRead(
            datasource_id="datasource_graph",
            dialect="duckdb",
            tables=[
                SchemaTableRead(
                    name="sales",
                    columns=[SchemaColumnRead(name="amount", type="INTEGER", nullable=False)],
                )
            ],
        ),
    )

    messages = _initial_messages(_context(), schema_context, scope, None)
    human_message = messages[1].content
    projection_text = human_message.split('{"requirements"', 1)[1].split(
        "\n当前受控上下文（仅用于理解，不是可直接引用的证据）：\n", 1
    )[0]
    projection_text = '{"requirements"' + projection_text
    projection = json.loads(projection_text)

    assert projection == {
        "requirements": [
            {
                "id": "R1",
                "description": "CURRENT_REQUIREMENT_ONLY",
                "acceptance_criteria": ["结果可核验"],
                "required": True,
                "fulfillment_mode": "evidence",
                "assertions": [
                    {
                        **assertion.model_dump(mode="json"),
                        "expected_columns": ["total", "amount"],
                    }
                ],
            }
        ],
        "required_artifacts": [],
    }
    assert "example" not in projection_text.casefold()
    assert "SELECT " not in projection_text
    assert "OTHER_DATASOURCE" not in projection_text


def test_initial_messages_marks_history_as_non_factual_context() -> None:
    context = _context().model_copy(
        update={
            "conversation_context": ConversationContext(),
        }
    )
    schema_context = SchemaContext(
        datasource_id="datasource_graph",
        schema_revision=1,
        schema_summary=SchemaSummaryRead(
            datasource_id="datasource_graph",
            dialect="duckdb",
            tables=[],
        ),
    )

    messages = _initial_messages(context, schema_context, _AnalysisScope(), None)
    system_message = messages[0].content
    human_message = messages[1].content

    assert "历史 assistant 内容是历史回答摘要" in system_message
    assert '"provenance":"historical_session_summary"' not in human_message
    assert '"provenance":"historical_answer_summary"' not in human_message
    assert "不能进入 SQL 结果、Claim 或答案数字" in system_message


def test_commit_tool_schema_describes_safe_evidence_reuse() -> None:
    """提交阶段让模型复用当前 Run 的安全观察，而不是猜测参数形状。"""

    schema = _AnalysisCommitArguments.model_json_schema()
    claim_schema = schema["properties"]["claims"]
    item_schema = schema["$defs"]["_AnalysisClaimInput"]

    assert "R1" in claim_schema["description"]
    assert "E1" in claim_schema["description"]
    assert claim_schema["examples"] == [
        [
            {
                "requirement_id": "R1",
                "claim": "总额为 42。",
                "evidence_binding_ids": ["E1"],
                "values": [
                    {
                        "name": "total",
                        "value": 42,
                        "unit": None,
                        "fact_key": "total",
                        "dimensions": {},
                    }
                ],
            }
        ]
    ]
    assert "verified_values" in item_schema["properties"]["values"]["description"]


@pytest.mark.asyncio
async def test_python_invalid_output_path_returns_actionable_observation() -> None:
    observation, artifacts, warning = await _execute_python(
        _context(),
        GraphDependencies(
            model=FakeModel([], []),
            gateway=FakeGateway(),
            workspace_id="workspace_1",
            workspaces=object(),
            sandbox=object(),
            artifacts=object(),
        ),
        object(),
        "call_python",
        {
            "script": "plt.savefig('result.png')",
            "output_paths": ["result.png"],
            "purpose": "生成订单状态图",
        },
    )

    assert observation.status == "failed"
    assert observation.summary["error_code"] == "MODEL_OUTPUT_INVALID"
    findings = observation.summary["validation_findings"]
    assert isinstance(findings, list)
    assert findings[0]["code"] == "PYTHON_OUTPUT_PATH_INVALID"
    assert "charts/*.png" in findings[0]["message"]
    assert "逐字一致" in findings[0]["message"]
    assert artifacts == []
    assert warning is None


@pytest.mark.asyncio
async def test_datalink_tool_rejects_unknown_focus_without_calling_mcp() -> None:
    """模型不能用实体名称冒充受控检索模式并被静默降级。"""

    response = DataLinkExploreResponse(result=_semantic_datalink_result(), cache_hit=False)
    datalink = FakeDataLink(response)
    context = _context().model_copy(update={"datalink_graph_version": "graph_1"})
    observation, artifacts, warning = await _execute_datalink(
        context,
        GraphDependencies(model=FakeModel([], []), gateway=FakeGateway(), datalink=datalink),
        _NeverCanceled(),
        "call_invalid_datalink",
        {"query": "loan_application", "focus": "loan_application", "max_nodes": 12},
    )

    assert observation.status == "failed"
    assert observation.summary["error_code"] == AgentErrorCode.MODEL_OUTPUT_INVALID.value
    assert artifacts == []
    assert warning is None
    assert datalink.requests == []


@pytest.mark.asyncio
async def test_datalink_tool_message_is_slim_while_summary_and_consumption_stay_full() -> None:
    recorded: list[object] = []

    async def record(item: object) -> None:
        recorded.append(item)

    response = DataLinkExploreResponse(result=_semantic_datalink_result(), cache_hit=False)
    context = _context().model_copy(update={"datalink_graph_version": "graph_1"})
    observation, artifacts, warning = await _execute_datalink(
        context,
        GraphDependencies(
            model=FakeModel([], []),
            gateway=FakeGateway(),
            datalink=FakeDataLink(response),
            record_datalink_consumption=record,
        ),
        _NeverCanceled(),
        "call_datalink",
        {"query": "客户订单", "max_nodes": 12},
    )

    assert observation.status == "succeeded"
    assert artifacts == []
    assert warning is None
    full_summary = json.dumps(observation.summary, ensure_ascii=False)
    assert "用于唯一标识下单客户的业务编号" in full_summary
    assert "private-customer-value" not in full_summary

    payload = json.loads(_tool_message_for_observation(observation).content)
    slim_context = payload["summary"]["semantic_context"]
    slim_text = json.dumps(slim_context, ensure_ascii=False)
    assert "semantic_catalog" not in slim_context
    assert "用于唯一标识下单客户的业务编号" not in slim_text
    assert "在系统中下单的个人或企业客户" not in slim_text
    field_names = {f"{field['table']}.{field['column']}" for field in slim_context["fields"]}
    assert "orders.customer_id" in field_names
    assert "客户标识" in slim_text
    assert "客户" in slim_text
    assert any(item.get("edge_type") == "foreign_key" for item in slim_context["relationships"])
    assert any(
        mapping.get("provenance") == "unknown" or mapping.get("concept_provenance")
        for field in slim_context["fields"]
        for mapping in field.get("semantic_mappings", [])
    )

    persisted = _persisted_tool_summary(observation)
    assert "orders.customer_id" in str(persisted.get("node_names"))
    assert persisted["semantic_entity_count"] == 1

    consumption = recorded[0]
    assert consumption.semantic_context is not None
    assert consumption.semantic_context.semantic_catalog.concepts
    assert any(field.description == "订单所属客户" for field in consumption.semantic_context.fields)


@pytest.mark.asyncio
async def test_sql_without_requirement_ids_is_rejected_before_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_missing_contract",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT 1"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="查询缺少目标归属"),
        ],
        [_answer("无法执行这条查询。", ["schema"])],
    )
    events = RecordingEvents()
    gateway = FakeGateway()

    await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert gateway.sql_requests == []
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    observation = json.loads(tool_messages[0].content)
    assert observation["summary"] == {
        "error_code": AgentErrorCode.MODEL_OUTPUT_INVALID.value,
        "reason_code": "ANALYSIS_SQL_CONTRACT_INVALID",
        "error_message": "这条 SQL 没有按当前分析目标声明，尚未执行。",
        "hint": (
            "请根据当前用户目标重新声明 requirement_ids；"
            "只有目标存在结构化检查时，才声明对应 assertion_ids。"
            "该请求没有进入 Data Gateway，未产生 SQL 审计或结果文件。"
        ),
        "retryable": True,
        "execution_status": "not_started",
        "validation_findings": [
            {
                "code": "ANALYSIS_REQUIREMENT_IDS_REQUIRED",
                "message": "SQL 必须声明这条查询服务的用户目标。",
            }
        ],
    }
    failed_event = next(event for event in events.events if event.type.value == "tool.failed")
    assert failed_event.payload == {
        "tool_call_id": "call_missing_contract",
        "tool_name": "run_sql_readonly",
        "turn_no": 1,
        "error_code": AgentErrorCode.MODEL_OUTPUT_INVALID.value,
        "reason_code": "ANALYSIS_SQL_CONTRACT_INVALID",
        "error_message": "这条 SQL 没有按当前分析目标声明，尚未执行。",
        "hint": (
            "请根据当前用户目标重新声明 requirement_ids；"
            "只有目标存在结构化检查时，才声明对应 assertion_ids。"
            "该请求没有进入 Data Gateway，未产生 SQL 审计或结果文件。"
        ),
        "retryable": True,
        "elapsed_ms": 0,
        "output_summary_json": failed_event.payload["output_summary_json"],
    }
    assert json.loads(failed_event.payload["output_summary_json"]) == {
        "error_code": AgentErrorCode.MODEL_OUTPUT_INVALID.value,
        "reason_code": "ANALYSIS_SQL_CONTRACT_INVALID",
        "retryable": True,
        "execution_status": "not_started",
        "validation_findings_json": (
            '[{"code":"ANALYSIS_REQUIREMENT_IDS_REQUIRED",'
            '"message":"SQL 必须声明这条查询服务的用户目标。"}]'
        ),
    }


@pytest.mark.asyncio
async def test_unbound_sql_then_empty_replies_stops_before_turn_limit() -> None:
    """没有绑定目标的 SQL 不能进网关，模型又不改正时也不能空转。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_missing_contract",
                        "name": "run_sql_readonly",
                        "args": {"sql": "SELECT amount AS total FROM sales"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="我先整理一下。"),
            AIMessage(content="我仍然没有执行查询。"),
        ],
        [_answer("查询尚未完成。", ["schema"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway()

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert gateway.sql_requests == []
    assert len(model.agent_messages) == 3
    invalid_sql_feedback = [
        item
        for item in model.agent_messages[1]
        if item.type == "tool" and item.tool_call_id == "call_missing_contract"
    ]
    assert "ANALYSIS_REQUIREMENT_IDS_REQUIRED" in invalid_sql_feedback[0].content
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_REQUIREMENTS_QUERY_REQUIRED"


@pytest.mark.asyncio
async def test_sql_with_unknown_assertion_is_rejected_before_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_unknown_assertion",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT 1",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R404.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="检查项不存在"),
        ],
        [_answer("无法执行这条查询。", ["schema"])],
    )
    gateway = FakeGateway()

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert gateway.sql_requests == []
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    assert "ANALYSIS_ASSERTION_NOT_FOUND:R404.A1" in tool_messages[0].content


@pytest.mark.asyncio
async def test_sql_with_multiple_requirements_must_select_each_requirement_assertion() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_missing_r2_assertion",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R1", "R2"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="第二个目标还没有对应检查项"),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("查询未执行。", ["schema"])],
    )
    model.plan = _materialized_plan("R1", "R2")
    gateway = FakeGateway()

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert gateway.sql_requests == []
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    assert "ANALYSIS_ASSERTION_IDS_REQUIRED:R2" in tool_messages[0].content


@pytest.mark.asyncio
async def test_sql_with_assertion_ids_only_derives_requirement_ids_for_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_assertion_only",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(requirement_id="R1"),
            AIMessage(content="已提交目标结论"),
        ],
        [_answer("金额为 42。", ["audit_42"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert len(gateway.sql_requests) == 1
    request = gateway.sql_requests[0]
    assert request.requirement_ids == ["R1"]
    assert request.assertion_ids == ["R1.A1"]
    assert request.expected_columns == ["total"]


@pytest.mark.asyncio
async def test_sql_can_bind_one_assertion_per_requirement_and_pass_expected_columns() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_multi_requirement",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R1", "R2"],
                            "assertion_ids": ["R1.A1", "R2.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(
                requirement_id="R1",
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            _commit_call(
                tool_call_id="commit_r2",
                requirement_id="R2",
                evidence_binding_ids=["E2"],
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            AIMessage(content="两个目标都已提交"),
        ],
        [_answer("两个目标均有结果。", ["audit_42"])],
    )
    model.plan = _materialized_plan("R1", "R2")
    gateway = FakeGateway([_sql_result(42)])

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert len(gateway.sql_requests) == 1
    request = gateway.sql_requests[0]
    assert request.requirement_ids == ["R1", "R2"]
    assert request.assertion_ids == ["R1.A1", "R2.A1"]
    assert request.expected_columns == ["total"]


@pytest.mark.asyncio
async def test_sql_contract_alias_mismatch_is_rejected_before_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_wrong_alias",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT COUNT(*) AS order_count FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="这条 SQL 未通过契约预检。"),
            AIMessage(content="当前没有可提交的结论。"),
        ],
        [_answer("尚未形成可验证结论。", ["schema"])],
    )
    model.plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="统计订单量",
            source_tables=["sales"],
            sql_constraints=[
                AnalysisSourceConstraint(kind="source", table="sales"),
                AnalysisAggregateConstraint(
                    kind="aggregate", function="count", column="*", alias="total_orders"
                ),
            ],
            claim_extractions=[
                {
                    "mode": "scalar",
                    "name": "订单总数",
                    "field": "total_orders",
                    "required": True,
                }
            ],
        )
    )
    gateway = FakeGateway([_sql_result(42)])
    events = RecordingEvents()

    await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert gateway.sql_requests == []
    tool_message = next(item for item in model.agent_messages[1] if item.type == "tool")
    observation = json.loads(tool_message.content)
    assert observation["summary"]["error_code"] == "ANALYSIS_SQL_CONTRACT_INVALID"
    assert observation["summary"]["execution_status"] == "not_started"
    assert observation["summary"]["retryable"] is False
    assert "当前调用不可原样重放" in observation["summary"]["recovery"]["instruction"]
    assert "实质不同的新 SQL" in observation["summary"]["recovery"]["instruction"]
    assert any(
        finding["code"] == "SQL_CONTRACT_AGGREGATE_MISMATCH:count"
        for finding in observation["summary"]["validation_findings"]
    )
    assert not any(event.type.value == "artifact.created" for event in events.events)


@pytest.mark.asyncio
async def test_nested_scope_contract_error_is_rejected_before_gateway() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_bad_scope",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": (
                                "WITH selected AS (SELECT amount FROM sales) "
                                "SELECT missing FROM selected"
                            ),
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="这条 SQL 的作用域无效。"),
            AIMessage(content="当前没有可提交的结论。"),
        ],
        [_answer("尚未形成可验证结论。", ["schema"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert gateway.sql_requests == []
    tool_message = next(item for item in model.agent_messages[1] if item.type == "tool")
    observation = json.loads(tool_message.content)
    assert observation["summary"]["error_code"] == "ANALYSIS_SQL_CONTRACT_INVALID"
    assert any(
        finding["code"] == "SQL_CONTRACT_SCOPE_INVALID"
        for finding in observation["summary"]["validation_findings"]
    )


@pytest.mark.asyncio
async def test_sql_contract_preflight_failure_stops_same_message_tool_batch() -> None:
    """契约预检失败后必须先让模型看到反馈，不能执行同一消息中的后续 SQL。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_invalid_contract", "SELECT amount AS wrong FROM sales"),
                    _tool_call("call_should_wait", "SELECT amount AS total FROM sales"),
                ],
            ),
            AIMessage(content="当前查询还没有形成可验证事实。"),
        ],
        [_answer("尚未形成可验证结论。")],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert gateway.sql_requests == []
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_invalid_contract"
    observation = json.loads(tool_messages[0].content)
    assert observation["summary"]["error_code"] == "ANALYSIS_SQL_CONTRACT_INVALID"
    assert observation["summary"]["execution_status"] == "not_started"


@pytest.mark.asyncio
async def test_sql_result_missing_expected_column_cannot_become_evidence() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_missing_result_column",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS missing_column FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="结果列不符合合同"),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("无法确认该指标。", ["schema"])],
    )
    model.plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="提取预期结果字段",
            source_tables=["sales"],
            claim_extractions=[
                {
                    "mode": "scalar",
                    "name": "缺失字段",
                    "field": "missing_column",
                    "required": True,
                }
            ],
        )
    )
    gateway = FakeGateway([_sql_result(42)])
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert len(gateway.sql_requests) == 1
    assert [artifact.artifact_id for artifact in state.outcome.artifact_refs] == []
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    assert "RESULT_EXPECTED_COLUMN_MISSING:missing_column" in tool_messages[0].content
    observation = json.loads(tool_messages[0].content)
    assert observation["status"] == "succeeded"
    assert observation["summary"]["validation_error_code"] == "ANALYSIS_RESULT_INVALID"
    assert not any(
        event.type.value == "artifact.created" and event.payload["artifact_id"] == "table_42"
        for event in events.events
    )
    assert any(
        event.type.value == "tool.succeeded"
        and event.payload["tool_call_id"] == "call_missing_result_column"
        for event in events.events
    )


@pytest.mark.asyncio
async def test_sql_result_validation_failure_blocks_the_same_sql_without_gateway_retry() -> None:
    """结果验证失败后，同一条已执行 SQL 不得再次进入 Data Gateway。"""

    sql = "SELECT amount AS total FROM sales"
    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_tool_call("call_invalid_result", sql)]),
            AIMessage(content="", tool_calls=[_tool_call("call_same_sql", f"{sql};")]),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("查询已执行，但没有形成可验证事实。")],
    )
    model.plan = _materialized_plan("R1")
    events = RecordingEvents()
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["total"], rows=[[41], [42]], row_count=2),
                audit_log_id="audit_invalid_result",
                artifact_id="table_invalid_result",
                elapsed_ms=1,
            )
        ]
    )

    await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["call_invalid_result"]
    first_feedback = next(
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.tool_call_id == "call_invalid_result"
    )
    first_observation = json.loads(first_feedback.content)
    assert first_observation["status"] == "succeeded"
    assert first_observation["summary"]["execution_status"] == "succeeded"
    assert first_observation["summary"]["validation_status"] == "failed"
    assert first_observation["summary"]["evidence_available"] is False
    assert "不要重复相同 SQL" in first_observation["summary"]["recovery"]
    same_feedback = next(
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.tool_call_id == "call_same_sql"
    )
    assert json.loads(same_feedback.content)["summary"]["error_code"] == (
        AgentErrorCode.SQL_REPAIR_SAME_STATEMENT.value
    )
    succeeded_event = next(
        event
        for event in events.events
        if event.type is RunEventType.TOOL_SUCCEEDED
        and event.payload["tool_call_id"] == "call_invalid_result"
    )
    assert json.loads(succeeded_event.payload["output_summary_json"]) == {
        "execution_status": "succeeded",
        "validation_status": "failed",
        "evidence_available": False,
        "validation_error_code": AgentErrorCode.ANALYSIS_RESULT_INVALID.value,
        "audit_log_id": "audit_invalid_result",
        "retryable": False,
        "validation_findings_json": (
            '[{"code":"RESULT_CLAIM_VALUE_MISSING:total",'
            '"message":"结果检查未通过：RESULT_CLAIM_VALUE_MISSING:total。"}]'
        ),
    }


@pytest.mark.asyncio
async def test_sql_result_validation_failure_allows_a_different_sql_repair() -> None:
    """结果验证失败后，实质不同 SQL 仍可用原 Audit 作为受控修复来源执行。"""

    initial_sql = "SELECT amount AS total FROM sales"
    rewritten_sql = "SELECT SUM(amount) AS total FROM sales"
    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_tool_call("call_invalid_result", initial_sql)]),
            AIMessage(content="", tool_calls=[_tool_call("call_rewritten", rewritten_sql)]),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
            AIMessage(content="结论已提交"),
        ],
        [_answer("总额为 42。")],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["total"], rows=[[41], [42]], row_count=2),
                audit_log_id="audit_invalid_result",
                artifact_id="table_invalid_result",
                elapsed_ms=1,
            ),
            _sql_result(42),
        ]
    )

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == [
        "call_invalid_result",
        "call_rewritten",
    ]
    assert gateway.sql_requests[1].repaired_from_audit_id == "audit_invalid_result"
    assert state.outcome.completion_kind == "completed"
    assert state.outcome.evidence_refs == ["audit_42", "table_42"]


@pytest.mark.asyncio
async def test_sql_result_failing_required_check_cannot_become_evidence() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_empty_result",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales WHERE 1 = 0",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="结果为空，需要修正查询"),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("没有足够数据确认该指标。", ["schema"])],
    )
    model.plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="结果必须非空",
            source_tables=["sales"],
            result_checks=[{"kind": "non_empty", "required": True}],
            claim_extractions=[
                {"mode": "scalar", "name": "total", "field": "total", "required": True}
            ],
        )
    )
    gateway = FakeGateway(
        [
            SqlExecutionResult(
                result=TableDataRead(columns=["total"], rows=[], row_count=0),
                audit_log_id="audit_empty",
                artifact_id="table_empty",
                elapsed_ms=1,
            )
        ]
    )

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [artifact.artifact_id for artifact in state.outcome.artifact_refs] == []
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    assert "RESULT_CHECK_NON_EMPTY_FAILED" in tool_messages[0].content


@pytest.mark.asyncio
async def test_sql_result_writes_verified_values_to_the_next_agent_turn() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_verified_total",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="总额已确认"),
            _commit_call(
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("call_after_commit", "SELECT * FROM unrelated_table")],
            ),
        ],
        [_answer("总额为 42。", ["audit_42"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert state.outcome.artifact_refs[0].artifact_id == "table_42"
    assert state.outcome.completion_kind == "completed"
    assert len(model.agent_messages) == 3
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    observation = json.loads(tool_messages[0].content)
    assert observation["summary"]["verified_values"] == [
        {
            "name": "total",
            "value": 42,
            "unit": None,
            "tolerance": 0.0,
            "assertion_id": "R1.A1",
            "fact_key": "total",
            "dimensions": {},
        }
    ]


@pytest.mark.asyncio
async def test_series_claim_commit_accepts_multiple_verified_dimension_values() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_series_query",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": (
                                "SELECT amount AS order_month, amount AS order_count FROM sales"
                            ),
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(
                values=[
                    {
                        "name": "订单数",
                        "value": 3,
                        "unit": None,
                        "fact_key": "order_count|order_month=2026-01",
                        "dimensions": {"order_month": "2026-01"},
                    },
                    {
                        "name": "订单数",
                        "value": 5,
                        "unit": None,
                        "fact_key": "order_count|order_month=2026-02",
                        "dimensions": {"order_month": "2026-02"},
                    },
                ]
            ),
        ],
        [_answer("2026 年 1 月 3 单，2 月 5 单。", ["audit_series"])],
    )
    model.plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="按月份统计订单数",
            claim_extractions=[
                {
                    "mode": "series",
                    "name": "订单数",
                    "value_field": "order_count",
                    "dimension_fields": ["order_month"],
                    "required": True,
                }
            ],
        )
    )

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_series_sql_result()]))
    )

    assert state.outcome.completion_kind == "completed"
    assert len(state.outcome.claim_audits) == 1
    assert [fact.value for fact in state.outcome.claim_audits[0].facts] == [3, 5]


@pytest.mark.asyncio
async def test_series_claim_commit_accepts_72_verified_dimension_values() -> None:
    rows = [[f"bucket-{index:03d}", index] for index in range(72)]
    claim_values = [
        {
            "name": "bucket_total",
            "value": index,
            "unit": None,
            "fact_key": f"total|bucket=bucket-{index:03d}",
            "dimensions": {"bucket": f"bucket-{index:03d}"},
        }
        for index in range(72)
    ]
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_large_series_query",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS bucket, amount AS total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(values=claim_values),
        ],
        [_answer("72 个分组事实均已核验。", ["audit_large_series"])],
    )
    model.plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="按分组统计数值",
            result_checks=[{"kind": "unique", "required": True, "fields": ["bucket"]}],
            claim_extractions=[
                {
                    "mode": "series",
                    "name": "bucket_total",
                    "value_field": "total",
                    "dimension_fields": ["bucket"],
                    "required": True,
                }
            ],
        )
    )
    result = SqlExecutionResult(
        result=TableDataRead(columns=["bucket", "total"], rows=rows, row_count=len(rows)),
        audit_log_id="audit_large_series",
        artifact_id="table_large_series",
        elapsed_ms=1,
    )

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([result]))
    )

    assert state.outcome.completion_kind == "completed"
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    observation = json.loads(tool_messages[0].content)
    assert len(observation["summary"]["verified_values"]) == 72
    assert len(state.outcome.claim_audits[0].facts) == 72


@pytest.mark.asyncio
async def test_series_claim_commit_fills_omitted_verified_dimension_values() -> None:
    """完整验证的 series 不要求模型在提交时复制整批分组事实。"""

    rows = [[f"bucket-{index:03d}", index] for index in range(72)]
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_omitted_series_query",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS bucket, amount AS total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(values=[]),
        ],
        [_answer("72 个分组事实均已核验。", ["audit_omitted_series"])],
    )
    model.plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="按分组统计数值",
            result_checks=[{"kind": "unique", "required": True, "fields": ["bucket"]}],
            claim_extractions=[
                {
                    "mode": "series",
                    "name": "bucket_total",
                    "value_field": "total",
                    "dimension_fields": ["bucket"],
                    "required": True,
                }
            ],
        )
    )
    result = SqlExecutionResult(
        result=TableDataRead(columns=["bucket", "total"], rows=rows, row_count=len(rows)),
        audit_log_id="audit_omitted_series",
        artifact_id="table_omitted_series",
        elapsed_ms=1,
    )

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([result]))
    )

    assert state.outcome.completion_kind == "completed"
    assert len(state.outcome.claim_audits) == 1
    assert len(state.outcome.claim_audits[0].facts) == 72
    assert [fact.value for fact in state.outcome.claim_audits[0].facts[:3]] == [0, 1, 2]


@pytest.mark.asyncio
async def test_sql_state_validation_failure_emits_tool_failure_before_run_failure(
    monkeypatch,
) -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_invalid_state", "SELECT amount AS total FROM sales")],
            )
        ],
        [],
    )
    events = RecordingEvents()

    def reject_progress(*_args):
        value = AnalysisVerifiedValue(
            name="total",
            value=1,
            assertion_id="R1.A1",
            fact_key="total",
        )
        AnalysisQueryAttempt(
            id="Q1",
            requirement_ids=["R1"],
            verified_values=[value] * 501,
        )

    monkeypatch.setattr("agent_runtime.graph._record_sql_analysis_progress", reject_progress)

    with pytest.raises(GraphRunError) as captured:
        await run_analysis_graph(
            _context(),
            GraphDependencies(
                model=model,
                gateway=FakeGateway([_sql_result(42)]),
                events=events,
            ),
        )

    assert captured.value.failure.code is AgentErrorCode.ANALYSIS_RESULT_INVALID
    assert [event.type for event in events.events][-2:] == [
        RunEventType.ARTIFACT_CREATED,
        RunEventType.TOOL_FAILED,
    ]
    assert events.events[-1].payload["error_code"] == AgentErrorCode.ANALYSIS_RESULT_INVALID.value


@pytest.mark.asyncio
async def test_evidenced_requirement_turn_only_exposes_commit_tool() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_r1", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
        ],
        [_answer("目标已确认。", ["audit_42"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert len(gateway.sql_requests) == 1
    assert model.bound_tool_names[1] == ["commit_analysis_claims"]
    assert "已有证据" in model.agent_messages[1][-1].content
    commit_prompt = model.agent_messages[1][0].content
    assert "当前阶段：证据提交" in commit_prompt
    assert "本回合允许工具：commit_analysis_claims" in commit_prompt
    assert "claim 时必须写出当前查询结果中与用户目标直接相关的具体数字" in commit_prompt
    final_messages = model.final_messages[0]
    assert len(final_messages) == 2
    final_snapshot = final_messages[1].content
    for hidden_value in (
        "SELECT amount AS total FROM sales",
        "verified_values",
        "R1",
        "audit_42",
        "table_42",
        "query_r1",
    ):
        assert hidden_value not in final_snapshot
    assert "row_count" not in final_snapshot
    assert "确认第 1 项结果" in final_snapshot
    assert "目标结论已由当前 Run 的证据确认" not in final_snapshot
    assert '"claim"' not in final_snapshot
    assert '"value":42' in final_snapshot
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_partial_assertion_evidence_reopens_sql_after_failed_commit() -> None:
    first_query = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "query_customer_total",
                "name": "run_sql_readonly",
                "args": {
                    "sql": "SELECT amount AS total FROM sales",
                    "requirement_ids": ["R1"],
                    "assertion_ids": ["R1.A1"],
                },
                "type": "tool_call",
            }
        ],
    )
    duplicate_first_query = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "repeat_customer_total",
                "name": "run_sql_readonly",
                "args": {
                    "sql": "SELECT amount AS total FROM sales",
                    "requirement_ids": ["R1"],
                    "assertion_ids": ["R1.A1"],
                },
                "type": "tool_call",
            }
        ],
    )
    second_query = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "query_channel_total",
                "name": "run_sql_readonly",
                "args": {
                    "sql": "SELECT amount AS channel_total FROM sales",
                    "requirement_ids": ["R1"],
                    "assertion_ids": ["R1.A2"],
                },
                "type": "tool_call",
            }
        ],
    )
    model = FakeModel(
        [
            first_query,
            _commit_call(
                tool_call_id="premature_commit",
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            duplicate_first_query,
            second_query,
            _commit_call(
                tool_call_id="complete_commit",
                evidence_binding_ids=["E1", "E2"],
                values=[
                    {"name": "total", "value": 42, "unit": None},
                    {"name": "channel_total", "value": 43, "unit": None},
                ],
            ),
        ],
        [_answer("客户总量和渠道分布均已核验。")],
    )
    model.plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="分析客户总量和渠道分布",
                acceptance_criteria=["两个检查项均有当前数据证据"],
                assertions=[
                    AnalysisAssertion(
                        id="R1.A1",
                        requirement_id="R1",
                        description="统计客户总量",
                        source_tables=["sales"],
                        claim_extractions=[
                            {
                                "mode": "scalar",
                                "name": "total",
                                "field": "total",
                                "required": True,
                            }
                        ],
                    ),
                    AnalysisAssertion(
                        id="R1.A2",
                        requirement_id="R1",
                        description="统计渠道分布",
                        source_tables=["sales"],
                        claim_extractions=[
                            {
                                "mode": "scalar",
                                "name": "channel_total",
                                "field": "channel_total",
                                "required": True,
                            }
                        ],
                    ),
                ],
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
    )
    gateway = FakeGateway(
        [
            _sql_result(42),
            SqlExecutionResult(
                result=TableDataRead(
                    columns=["channel_total"],
                    rows=[[43]],
                    row_count=1,
                ),
                audit_log_id="audit_channel_total",
                artifact_id="table_channel_total",
                elapsed_ms=1,
            ),
        ]
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway),
    )

    assert [request.assertion_ids for request in gateway.sql_requests] == [
        ["R1.A1"],
        ["R1.A2"],
    ]
    assert "run_sql_readonly" in model.bound_tool_names[1]
    duplicate_feedback = next(
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.tool_call_id == "repeat_customer_total"
    )
    assert "ANALYSIS_ASSERTION_ALREADY_EVIDENCED:R1.A1" in duplicate_feedback.content
    premature_feedback = next(
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.tool_call_id == "premature_commit"
    )
    premature_observation = json.loads(premature_feedback.content)
    assert premature_observation["status"] == "succeeded"
    assert premature_observation["summary"]["target_status"] == "queried"
    assert (state.outcome.completion_kind, state.outcome.incomplete_reason) == ("completed", None)


@pytest.mark.asyncio
async def test_same_requirement_can_commit_claims_per_assertion() -> None:
    """同一目标的不同证据可以分多个 Claim 提交后再标记完成。"""

    plan = MaterializedAnalysisPlan(
        mode="ready",
        requirements=(
            AnalysisRequirement(
                id="R1",
                description="分析客户总量和渠道分布",
                acceptance_criteria=["两个检查项均有当前数据证据"],
                assertions=[
                    AnalysisAssertion(
                        id="R1.A1",
                        requirement_id="R1",
                        description="统计客户总量",
                        source_tables=["sales"],
                        claim_extractions=[
                            {
                                "mode": "scalar",
                                "name": "total",
                                "field": "total",
                                "required": True,
                            }
                        ],
                    ),
                    AnalysisAssertion(
                        id="R1.A2",
                        requirement_id="R1",
                        description="统计渠道分布",
                        source_tables=["sales"],
                        claim_extractions=[
                            {
                                "mode": "scalar",
                                "name": "channel_total",
                                "field": "channel_total",
                                "required": True,
                            }
                        ],
                    ),
                ],
            ),
        ),
        constraints=AnalysisExecutionConstraints(),
    )
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_total", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        **_tool_call(
                            "query_channel_total", "SELECT amount AS channel_total FROM sales"
                        ),
                        "args": {
                            "sql": "SELECT amount AS channel_total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A2"],
                        },
                    }
                ],
            ),
            _commit_call(
                tool_call_id="commit_total",
                evidence_binding_ids=["E1"],
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            _commit_call(
                tool_call_id="commit_channel_total",
                evidence_binding_ids=[],
                values=[{"name": "channel_total", "value": 43, "unit": None}],
            ),
        ],
        [_answer("客户总量为 42，渠道分布为 43。")],
    )
    model.plan = plan
    gateway = FakeGateway(
        [
            _sql_result(42),
            SqlExecutionResult(
                result=TableDataRead(columns=["channel_total"], rows=[[43]], row_count=1),
                audit_log_id="audit_channel_total",
                artifact_id="table_channel_total",
                elapsed_ms=1,
            ),
        ]
    )
    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, plan=plan)
    )
    assert state.outcome.completion_kind == "completed"
    assert len(state.outcome.claim_audits) == 2
    assert [audit.requirement_id for audit in state.outcome.claim_audits] == ["R1", "R1"]
    assert [audit.facts[0].value for audit in state.outcome.claim_audits] == [42, 43]
    first_commit_observation = next(
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.tool_call_id == "commit_total"
    )
    assert json.loads(first_commit_observation.content)["status"] == "succeeded"


def test_bound_commit_observation_keeps_binding_ownership() -> None:
    message = ToolMessage(
        content=json.dumps(
            {
                "status": "succeeded",
                "target_status": "mixed",
                "evidence_binding_ids": ["E3", "E4", "E6", "E7"],
                "claim_details": [
                    {
                        "requirement_id": "R2",
                        "evidence_binding_ids": ["E3", "E4"],
                        "assertion_ids": ["R2.A1", "R2.A2"],
                        "new_assertion_ids": ["R2.A1", "R2.A2"],
                    },
                    {
                        "requirement_id": "R3",
                        "evidence_binding_ids": ["E6", "E7"],
                        "assertion_ids": ["R3.A1", "R3.A2"],
                        "new_assertion_ids": ["R3.A1", "R3.A2"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        tool_call_id="commit_mixed",
    )

    bounded = json.loads(_bound_tool_message(message).content)

    assert bounded["claim_details"][0]["evidence_binding_ids"] == ["E3", "E4"]
    assert bounded["claim_details"][1]["evidence_binding_ids"] == ["E6", "E7"]


@pytest.mark.asyncio
async def test_metadata_query_returns_recovery_without_gateway_audit_or_artifact() -> None:
    """取证后误查系统表时，模型必须收到具体原因并能直接提交已有结论。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_total", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "query_metadata",
                        "SELECT column_name FROM information_schema.columns",
                    )
                ],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
        ],
        [_answer("金额为 42。")],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=gateway, events=events),
    )

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_total"]
    feedback = next(
        message
        for messages in model.agent_messages
        for message in messages
        if message.type == "tool" and message.tool_call_id == "query_metadata"
    )
    observation = json.loads(feedback.content)
    assert observation["summary"] == {
        "error_code": "SCHEMA_REDUNDANT_METADATA_QUERY",
        "reason_code": "SCHEMA_REDUNDANT_METADATA_QUERY",
        "error_message": "当前 Run 已提供数据结构，系统元数据查询不会产生新的业务证据。",
        "subject_kind": "system_metadata",
        "subject": "当前数据源的系统元数据",
        "hint": (
            "不要重发或改写系统元数据查询。字段、类型和所属表请直接使用当前 Schema；"
            "实体与字段的对应请依据已提供的 DataLink 推断线索自行组织。"
            "当前 Run 已有验证过的数据结果，"
            "请基于已有 ToolMessage 中的 verified_values 组织 Claim，"
            "然后调用 commit_analysis_claims。该请求没有进入 Data Gateway，"
            "未产生 SQL 审计或结果文件。"
        ),
        "next_action": "commit_analysis_claims",
        "retryable": False,
        "execution_status": "not_started",
    }
    failed_event = next(
        event
        for event in events.events
        if event.type.value == "tool.failed" and event.payload["tool_call_id"] == "query_metadata"
    )
    assert failed_event.payload["reason_code"] == "SCHEMA_REDUNDANT_METADATA_QUERY"
    assert "未产生 SQL 审计或结果文件" in failed_event.payload["hint"]
    assert state.outcome.completion_kind == "completed"


@pytest.mark.asyncio
async def test_repeated_metadata_query_finishes_with_commit_required() -> None:
    """同一 Run 再次忽略恢复提示时，不能耗到总回合上限。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_total", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_metadata_1", "PRAGMA table_info('sales')")],
            ),
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_metadata_2", "SELECT * FROM sqlite_master")],
            ),
        ],
        [_answer("已有结果但未提交结论。")],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_total"]
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_REQUIREMENTS_COMMIT_REQUIRED"


@pytest.mark.asyncio
async def test_uncommitted_requirement_cannot_be_finalized_as_completed() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_r1", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(content="我先整理结果"),
            AIMessage(content="我仍然不提交结论"),
        ],
        [_answer("结果已整理，但结论尚未提交。", ["audit_42"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert model.bound_tool_names[1] == ["commit_analysis_claims"]
    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_REQUIREMENTS_COMMIT_REQUIRED"
    assert state.outcome.evidence_refs == []
    assert state.outcome.answer == "结果已整理，但结论尚未提交。"
    final_snapshot = json.loads(model.final_messages[0][1].content)
    assert final_snapshot["confirmed_facts"] == []
    assert final_snapshot["verified_evidence"][0]["values"][0]["value"] == 42


@pytest.mark.asyncio
async def test_claim_timeout_keeps_verified_values_in_partial_answer() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_r1", "SELECT amount AS total FROM sales")],
            ),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_CLAIM_COMMIT_TIMEOUT,
                message="结论提交模型请求超过本阶段时限",
            ),
        ],
        [_answer("查询结果已得到，但结论提交超时。")],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_CLAIM_COMMIT_TIMEOUT"
    assert state.outcome.evidence_refs == []
    final_snapshot = json.loads(model.final_messages[0][1].content)
    assert final_snapshot["confirmed_facts"] == []
    assert final_snapshot["verified_evidence"][0]["values"][0]["value"] == 42


@pytest.mark.asyncio
async def test_pending_requirement_query_remains_allowed_after_evidence() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_r1", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(
                requirement_id="R1",
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "query_r2",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R2"],
                            "assertion_ids": ["R2.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(
                tool_call_id="commit_r2",
                requirement_id="R2",
                evidence_binding_ids=["E2"],
                values=[{"name": "total", "value": 43, "unit": None}],
            ),
        ],
        [_answer("两个目标都已确认。", ["audit_42", "audit_43"])],
    )
    model.plan = _materialized_plan("R1", "R2")
    gateway = FakeGateway([_sql_result(42), _sql_result(43)])
    events = RecordingEvents()

    await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert [request.requirement_ids for request in gateway.sql_requests] == [["R1"], ["R2"]]
    commit_summaries = [
        json.loads(event.payload["output_summary_json"])
        for event in events.events
        if event.type is RunEventType.TOOL_SUCCEEDED
        and event.payload.get("tool_name") == "commit_analysis_claims"
    ]
    assert [item["requirement_ids"] for item in commit_summaries] == ["R1", "R2"]
    assert all(item["target_status"] == "reported" for item in commit_summaries)
    assert all("completion_digest" in item for item in commit_summaries)


@pytest.mark.asyncio
async def test_blocked_assertion_does_not_keep_partially_reported_requirement_queryable() -> None:
    plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="读取可用总额",
            source_tables=["sales"],
            claim_extractions=[
                {
                    "mode": "scalar",
                    "name": "total",
                    "field": "total",
                    "required": True,
                }
            ],
        )
    )
    requirement = plan.requirements[0]
    blocked_assertion = AnalysisAssertion(
        id="R1.A2",
        requirement_id="R1",
        description="统计记录数",
        source_tables=["sales"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="sales"),
            AnalysisAggregateConstraint(
                kind="aggregate", function="count", column="*", alias="record_count"
            ),
        ],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "record_count",
                "field": "record_count",
                "required": True,
            }
        ],
    )
    plan = MaterializedAnalysisPlan(
        mode=plan.mode,
        requirements=(
            requirement.model_copy(
                update={"assertions": [*requirement.assertions, blocked_assertion]}
            ),
        ),
        constraints=plan.constraints,
    )
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("query_viable_assertion", "SELECT amount AS total FROM sales")
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "invalid_blocked_assertion_1",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT COUNT(*) AS wrong_count FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A2"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "invalid_blocked_assertion_2",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT COUNT(amount) AS wrong_count FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A2"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            _commit_call(
                tool_call_id="commit_viable_assertion",
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
        ],
        [_answer("当前可验证的总额为 42；记录数检查未能完成。")],
    )
    events = RecordingEvents()

    gateway = FakeGateway([_sql_result(42)])
    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=gateway,
            events=events,
            plan=plan,
        ),
    )

    assert state.outcome.completion_kind == "completed"
    assert state.outcome.incomplete_reason is None
    assert state.outcome.answer == "当前可验证的总额为 42；记录数检查未能完成。"
    assert state.blocked_assertion_count == 1
    assert len(state.outcome.claim_audits) == 1
    assert [request.tool_call_id for request in gateway.sql_requests] == ["query_viable_assertion"]
    commit_summary = next(
        json.loads(event.payload["output_summary_json"])
        for event in events.events
        if event.type is RunEventType.TOOL_SUCCEEDED
        and event.payload.get("tool_call_id") == "commit_viable_assertion"
    )
    assert commit_summary["target_status"] == "queried"
    final_snapshot = json.loads(model.final_messages[0][1].content)
    assert final_snapshot["confirmed_facts"][0]["values"][0]["value"] == 42
    assert final_snapshot["incomplete_goals"] == []
    assert any("停止继续修复" in warning for warning in final_snapshot["warnings"])


@pytest.mark.asyncio
async def test_fully_blocked_requirement_without_claim_remains_partial() -> None:
    plan = _plan_with_assertion(
        AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="统计记录数",
            source_tables=["sales"],
            sql_constraints=[
                AnalysisSourceConstraint(kind="source", table="sales"),
                AnalysisAggregateConstraint(
                    kind="aggregate", function="count", column="*", alias="record_count"
                ),
            ],
            claim_extractions=[
                {
                    "mode": "scalar",
                    "name": "record_count",
                    "field": "record_count",
                    "required": True,
                }
            ],
        )
    )
    invalid_calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"invalid_only_assertion_{index}",
                    "name": "run_sql_readonly",
                    "args": {
                        "sql": sql,
                        "requirement_ids": ["R1"],
                        "assertion_ids": ["R1.A1"],
                    },
                    "type": "tool_call",
                }
            ],
        )
        for index, sql in enumerate(
            (
                "SELECT COUNT(*) AS wrong_count FROM sales",
                "SELECT COUNT(amount) AS wrong_count FROM sales",
            ),
            start=1,
        )
    ]
    model = FakeModel(
        [
            *invalid_calls,
            AIMessage(content="该检查无法继续。"),
            AIMessage(content="请如实说明没有形成正式结论。"),
        ],
        [_answer("本次没有形成可验证结论。")],
    )
    gateway = FakeGateway()

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, plan=plan)
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_REQUIREMENTS_BLOCKED"
    assert state.blocked_assertion_count == 1
    assert state.outcome.claim_audits == []
    assert gateway.sql_requests == []


@pytest.mark.asyncio
async def test_sql_for_reported_requirement_is_blocked_while_another_requirement_is_pending() -> (
    None
):
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_r1", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
            AIMessage(
                content="",
                tool_calls=[_tool_call("repeat_reported_r1", "SELECT amount AS total FROM sales")],
            ),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("R1 已提交，R2 尚未完成。", ["schema"])],
    )
    model.plan = _materialized_plan("R1", "R2")
    events = RecordingEvents()
    gateway = FakeGateway([_sql_result(42)])

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert len(gateway.sql_requests) == 1
    blocked_messages = [
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.name == "run_sql_readonly"
    ]
    assert "ANALYSIS_REQUIREMENT_ALREADY_REPORTED:R1" in blocked_messages[-1].content
    blocked_event = next(
        event
        for event in events.events
        if event.type.value == "tool.failed"
        and event.payload["tool_call_id"] == "repeat_reported_r1"
    )
    assert blocked_event.payload["reason_code"] == "ANALYSIS_SQL_ACTION_BLOCKED"
    assert blocked_event.payload["retryable"] is True
    assert "R1" not in json.dumps(blocked_event.payload, ensure_ascii=False)
    assert state.outcome.completion_kind == "partial"


@pytest.mark.asyncio
async def test_commit_rejects_evidence_from_another_requirement() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_query_for_commit",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(
                evidence_binding_ids=["E2"],
                values=[{"name": "total", "value": 42, "unit": None}],
            ),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("当前只完成了查询，结论尚未提交。", ["schema"])],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    assert state.outcome.completion_kind == "partial"
    commit_messages = [
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.name == "commit_analysis_claims"
    ]
    assert "ANALYSIS_REQUIREMENT_EVIDENCE_INVALID:R1:E2" in commit_messages[0].content


@pytest.mark.asyncio
async def test_commit_rejects_value_that_differs_from_verified_result() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_query_for_value",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(values=[{"name": "total", "value": 41, "unit": None}]),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("数值未通过校验，暂不形成完整结论。", ["schema"])],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    assert state.outcome.completion_kind == "partial"
    commit_messages = [
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.name == "commit_analysis_claims"
    ]
    assert "ANALYSIS_CLAIM_VALUE_MISMATCH:R1:total" in commit_messages[0].content


@pytest.mark.asyncio
async def test_repeated_invalid_commit_stops_with_specific_partial_reason() -> None:
    """内部提交参数连续无效时，不能反复消耗整个 Run 的分析窗口。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_for_commit", "SELECT amount AS total FROM sales")],
            ),
            _commit_call("invalid_commit_1", evidence_binding_ids=["E2"]),
            _commit_call("invalid_commit_2", evidence_binding_ids=["E2"]),
        ],
        [_answer("已有查询结果，但结论未能通过提交校验。")],
    )
    model.plan = _materialized_plan("R1")
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]), events=events),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_CLAIM_COMMIT_RETRY_EXHAUSTED"
    assert len(model.agent_messages) == 3
    assert [tool_names for tool_names in model.bound_tool_names[1:]] == [
        ["commit_analysis_claims"],
        ["commit_analysis_claims"],
    ]
    invalid_commit_feedback = [
        message
        for messages in model.agent_messages
        for message in messages
        if message.type == "tool" and message.name == "commit_analysis_claims"
    ]
    assert len(invalid_commit_feedback) == 1
    assert "ANALYSIS_REQUIREMENT_EVIDENCE_INVALID:R1:E2" in invalid_commit_feedback[0].content
    commit_failed_events = [
        event
        for event in events.events
        if event.type is RunEventType.TOOL_FAILED
        and event.payload.get("tool_name") == "commit_analysis_claims"
    ]
    assert len(commit_failed_events) == 2
    assert [event.payload["tool_call_id"] for event in commit_failed_events] == [
        "invalid_commit_1",
        "invalid_commit_2",
    ]
    assert all(
        event.payload["error_code"] == "MODEL_OUTPUT_INVALID" for event in commit_failed_events
    )


@pytest.mark.asyncio
async def test_commit_stage_timeout_is_not_reported_as_run_timeout() -> None:
    """提交专用模型窗口耗尽时保留流程事实，不能误称整轮 Run 已超时。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_for_commit", "SELECT amount AS total FROM sales")],
            ),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_CLAIM_COMMIT_TIMEOUT,
                message="结论提交模型请求超过本阶段时限",
            ),
        ],
        [_answer("已有查询结果，但提交阶段未能完成。")],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_CLAIM_COMMIT_TIMEOUT"
    assert state.outcome.answer == "已有查询结果，但提交阶段未能完成。"
    final_snapshot = json.loads(model.final_messages[0][1].content)
    assert final_snapshot["confirmed_facts"] == []
    assert final_snapshot["verified_evidence"][0]["values"][0]["value"] == 42


@pytest.mark.asyncio
async def test_commit_requires_all_verified_values_declared_by_requirement() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_query_for_required_value",
                        "name": "run_sql_readonly",
                        "args": {
                            "sql": "SELECT amount AS total FROM sales",
                            "requirement_ids": ["R1"],
                            "assertion_ids": ["R1.A1"],
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(values=[]),
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="停止继续尝试",
            ),
        ],
        [_answer("已查到结果，但必需数值尚未提交。", ["schema"])],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    assert state.outcome.completion_kind == "partial"
    commit_messages = [
        item
        for messages in model.agent_messages
        for item in messages
        if item.type == "tool" and item.name == "commit_analysis_claims"
    ]
    assert "ANALYSIS_CLAIM_VALUE_REQUIRED:R1:total" in commit_messages[0].content


@pytest.mark.asyncio
async def test_same_batch_runs_native_tool_calls_in_returned_order_after_normal_failure() -> None:
    events = RecordingEvents()
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_failed", "SELECT amount AS total FROM sales"),
                    _tool_call("call_succeeded", "SELECT amount AS total FROM sales"),
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(evidence_binding_ids=["E1"]),
            AIMessage(content="已提交结论"),
        ],
        [_answer("总额为 42。", ["audit_42"])],
    )
    gateway = FakeGateway(
        [
            AgentFailure(code=AgentErrorCode.DATA_GATEWAY_FAILED, message="safe failure"),
            _sql_result(42),
        ]
    )

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert [item.tool_call_id for item in gateway.sql_requests] == ["call_failed", "call_succeeded"]
    assert gateway.sql_requests[0].requirement_ids == ["R1"]
    assert gateway.sql_requests[0].assertion_ids == ["R1.A1"]
    assert state.outcome.evidence_refs == ["audit_42", "table_42"]
    tool_messages = [item for item in model.agent_messages[1] if item.type == "tool"]
    assert [item.tool_call_id for item in tool_messages] == ["call_failed", "call_succeeded"]
    called_events = [event for event in events.events if event.type.value == "tool.called"]
    assert [event.payload["tool_call_id"] for event in called_events] == [
        "call_failed",
        "call_succeeded",
        "commit_r1",
    ]
    assert [event.tool_input for event in called_events[:2]] == [
        {
            "sql": "SELECT amount AS total FROM sales",
            "requirement_ids": ["R1"],
            "assertion_ids": ["R1.A1"],
        },
        {
            "sql": "SELECT amount AS total FROM sales",
            "requirement_ids": ["R1"],
            "assertion_ids": ["R1.A1"],
        },
    ]
    assert all("tool_input" not in event.model_dump() for event in called_events)


@pytest.mark.asyncio
async def test_second_tool_cannot_start_until_first_tool_message_is_ready() -> None:
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    second_started = False

    class BlockingGateway(FakeGateway):
        async def execute_readonly(self, request, _cancellation):
            nonlocal second_started
            self.sql_requests.append(request)
            if len(self.sql_requests) == 1:
                first_started.set()
                await allow_first_to_finish.wait()
            else:
                second_started = True
            return _sql_result(len(self.sql_requests))

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_first", "SELECT amount AS total FROM sales"),
                    _tool_call("call_second", "SELECT amount AS total FROM sales"),
                ],
            ),
            AIMessage(content="查询完成"),
            _commit_call(evidence_binding_ids=["E1"]),
            AIMessage(content="已提交结论"),
        ],
        [_answer("查询已完成。", ["audit_2"])],
    )
    gateway = BlockingGateway()
    task = asyncio.create_task(
        run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))
    )

    await first_started.wait()
    await asyncio.sleep(0)
    assert second_started is False
    allow_first_to_finish.set()
    await task

    assert [item.tool_call_id for item in gateway.sql_requests] == ["call_first", "call_second"]


@pytest.mark.asyncio
async def test_safety_block_stops_remaining_native_tool_calls() -> None:
    events = RecordingEvents()
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_blocked", "DROP TABLE sales"),
                    _tool_call("call_never_runs", "SELECT * FROM sales"),
                ],
            )
        ],
        [],
    )
    gateway = FakeGateway(
        [AgentFailure(code=AgentErrorCode.DATA_GATEWAY_BLOCKED, message="blocked")]
    )

    with pytest.raises(GraphRunError) as caught:
        await run_analysis_graph(
            _context(),
            GraphDependencies(model=model, gateway=gateway, events=events),
        )

    assert caught.value.failure.code is AgentErrorCode.DATA_GATEWAY_BLOCKED
    assert [item.tool_call_id for item in gateway.sql_requests] == ["call_blocked"]
    assert [event.type.value for event in events.events][-1] == "tool.failed"


@pytest.mark.asyncio
async def test_retryable_sql_guard_failure_returns_feedback_then_uses_a_linked_rewrite() -> None:
    events = RecordingEvents()
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_rejected", "SELECT AVG(not.fully.paid) FROM sales"),
                    _tool_call("call_not_started", "SELECT SUM(amount) FROM sales"),
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "call_rewritten", 'SELECT AVG("not.fully.paid") AS total FROM sales'
                    ),
                ],
            ),
            _commit_call(
                evidence_binding_ids=["E1"],
                values=[{"name": "total", "value": 2, "unit": None}],
            ),
            AIMessage(content="查询结论已提交"),
        ],
        [_answer("已按修正后的 SQL 完成查询。")],
    )
    gateway = FakeGateway([_sql_guard_failure(audit_log_id="audit_rejected"), _sql_result(2)])

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=gateway, events=events)
    )

    assert state.outcome.completion_kind == "completed"
    assert [request.tool_call_id for request in gateway.sql_requests] == [
        "call_rejected",
        "call_rewritten",
    ]
    assert gateway.sql_requests[1].repaired_from_audit_id == "audit_rejected"
    feedback = [message for message in model.agent_messages[1] if message.type == "tool"]
    assert len(feedback) == 1
    assert "SQL_PARSE_ERROR" in str(feedback[0].content)
    assert "not.fully.paid" in str(feedback[0].content)
    assert "call_not_started" not in [request.tool_call_id for request in gateway.sql_requests]
    assert [
        event.payload["tool_call_id"]
        for event in events.events
        if event.type.value == "tool.called"
    ] == [
        "call_rejected",
        "call_rewritten",
        "commit_r1",
    ]


@pytest.mark.asyncio
async def test_same_sql_after_retryable_guard_failure_does_not_create_another_audit_attempt() -> (
    None
):
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("call_rejected", "SELECT AVG(not.fully.paid) FROM sales")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_same", "SELECT AVG(not.fully.paid) FROM sales;"),
                    _tool_call("call_after_same", "SELECT SUM(amount) FROM sales"),
                ],
            ),
            AIMessage(content="停止继续查询"),
        ],
        [_answer("本次未形成可验证结论。")],
    )
    gateway = FakeGateway([_sql_guard_failure(audit_log_id="audit_rejected")])

    await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert [request.tool_call_id for request in gateway.sql_requests] == ["call_rejected"]
    feedback = [message for message in model.agent_messages[2] if message.type == "tool"]
    assert len(feedback) == 2
    assert "SQL_REPAIR_SAME_STATEMENT" in str(feedback[-1].content)


@pytest.mark.asyncio
async def test_repeated_same_sql_consumes_repair_budget_without_gateway_retry() -> None:
    """重复原 SQL 也要消耗修复次数，达到上限后不再创建 Gateway 尝试。"""

    sql = "SELECT AVG(not.fully.paid) FROM sales"
    gateway = FakeGateway()
    dependencies = GraphDependencies(model=FakeModel([], []), gateway=gateway)
    requirements = [
        AnalysisRequirement(
            id="R1",
            description="确认结果",
            acceptance_criteria=["结果可核验"],
        )
    ]
    pending = _PendingSqlRepair(
        audit_log_id="audit_initial",
        sql_fingerprint=_sql_fingerprint(sql),
        repair_count=0,
    )

    first = await _execute_sql(
        _context(),
        dependencies,
        _NeverCanceled(),
        "call_same_1",
        {
            "sql": sql,
            "requirement_ids": ["R1"],
        },
        requirements,
        pending,
    )
    assert first[0].summary["error_code"] == AgentErrorCode.SQL_REPAIR_SAME_STATEMENT.value
    assert first[-1] is not None
    assert first[-1].pending is not None
    assert first[-1].pending.repair_count == 1

    second = await _execute_sql(
        _context(),
        dependencies,
        _NeverCanceled(),
        "call_same_2",
        {
            "sql": f"{sql};",
            "requirement_ids": ["R1"],
        },
        requirements,
        first[-1].pending,
    )

    assert second[0].summary["error_code"] == AgentErrorCode.SQL_REPAIR_LIMIT_REACHED.value
    assert second[-1] is not None
    assert second[-1].completion_reason == "SQL_REPAIR_LIMIT_REACHED"
    assert gateway.sql_requests == []


@pytest.mark.asyncio
async def test_repeated_preflight_invalid_sql_consumes_repair_budget_without_gateway_retry() -> (
    None
):
    """契约预检失败也必须进入同一条 SQL repair 状态，不能无限重放。"""

    sql = "SELECT COUNT(*) AS order_count FROM sales"
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["sales"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="sales"),
            AnalysisAggregateConstraint(
                kind="aggregate", function="count", column="*", alias="total_orders"
            ),
        ],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "订单总数",
                "field": "total_orders",
                "required": True,
            }
        ],
    )
    requirements = [
        AnalysisRequirement(
            id="R1",
            description="统计订单量",
            acceptance_criteria=["结果可核验"],
            assertions=[assertion],
        )
    ]
    gateway = FakeGateway()
    dependencies = GraphDependencies(model=FakeModel([], []), gateway=gateway)

    first = await _execute_sql(
        _context(),
        dependencies,
        _NeverCanceled(),
        "preflight_1",
        {
            "sql": sql,
            "requirement_ids": ["R1"],
            "assertion_ids": ["R1.A1"],
        },
        requirements,
        None,
    )
    assert first[0].summary["error_code"] == AgentErrorCode.ANALYSIS_SQL_CONTRACT_INVALID.value
    assert first[0].summary["retryable"] is False
    assert first[0].summary["execution_status"] == "not_started"
    assert "不可原样重放" in first[0].summary["recovery"]["instruction"]
    assert first[1] == []
    assert first[3] is not None
    assert first[4] is not None
    assert first[4].pending is not None
    assert first[4].pending.repair_count == 0

    second = await _execute_sql(
        _context(),
        dependencies,
        _NeverCanceled(),
        "preflight_same",
        {"sql": f"{sql};", "requirement_ids": ["R1"], "assertion_ids": ["R1.A1"]},
        requirements,
        first[4].pending,
    )

    assert second[0].summary["error_code"] == AgentErrorCode.SQL_REPAIR_SAME_STATEMENT.value
    assert second[4] is not None
    assert second[4].pending is not None
    assert second[4].pending.repair_count == 1
    assert gateway.sql_requests == []


@pytest.mark.asyncio
async def test_retryable_gateway_failure_returns_repairable_sql_observation() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计总额",
        source_tables=["sales"],
        claim_extractions=[{"mode": "scalar", "name": "总额", "field": "total", "required": True}],
    )
    requirements = [
        AnalysisRequirement(
            id="R1",
            description="统计总额",
            acceptance_criteria=["结果可核验"],
            assertions=[assertion],
        )
    ]
    gateway = FakeGateway(
        [
            AgentFailure(
                code=AgentErrorCode.DATA_GATEWAY_FAILED,
                message="SQL 查询失败",
                retryable=True,
            )
        ]
    )

    observation, _artifacts, _warning, _progress, repair = await _execute_sql(
        _context(),
        GraphDependencies(model=FakeModel([], []), gateway=gateway),
        _NeverCanceled(),
        "gateway_retryable",
        {
            "sql": "SELECT amount AS total FROM sales",
            "requirement_ids": ["R1"],
            "assertion_ids": ["R1.A1"],
        },
        requirements,
        None,
    )

    assert observation.summary["error_code"] == AgentErrorCode.DATA_GATEWAY_FAILED.value
    assert observation.summary["reason_code"] == "QUERY_FAILED"
    assert observation.summary["retryable"] is True
    assert repair is not None and repair.pending is not None
    assert gateway.sql_requests


@pytest.mark.asyncio
async def test_sql_gateway_receives_contract_columns_when_model_omits_expected_columns() -> None:
    """正式契约字段是 Gateway/结果校验的事实来源，模型声明可以为空。"""

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计总额",
        source_tables=["sales"],
        claim_extractions=[{"mode": "scalar", "name": "总额", "field": "total", "required": True}],
    )
    requirements = [
        AnalysisRequirement(
            id="R1",
            description="统计总额",
            acceptance_criteria=["结果可核验"],
            assertions=[assertion],
        )
    ]
    gateway = FakeGateway([_sql_result(42)])
    result = await _execute_sql(
        _context(),
        GraphDependencies(model=FakeModel([], []), gateway=gateway),
        _NeverCanceled(),
        "contract_columns",
        {
            "sql": "SELECT amount AS total FROM sales",
            "requirement_ids": ["R1"],
            "assertion_ids": ["R1.A1"],
        },
        requirements,
        None,
    )

    assert result[0].status == "succeeded"
    assert gateway.sql_requests[0].expected_columns == ["total"]
    assert result[3] is not None
    assert result[3].expected_columns == ["total"]


@pytest.mark.asyncio
async def test_different_sql_can_use_gateway_after_same_sql_repair_feedback() -> None:
    """相同 SQL 的一次受控反馈后，实质不同的 SQL 仍可进入 Gateway。"""

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_initial", "SELECT AVG(not.fully.paid) AS total FROM sales")
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "call_rewrite",
                        'SELECT AVG("not.fully.paid") AS total FROM sales',
                    )
                ],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
        ],
        [_answer("已完成查询。", ["audit_42"])],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway(
        [
            _sql_guard_failure(audit_log_id="audit_initial"),
            _sql_result(42),
        ]
    )

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert state.outcome.completion_kind == "completed"
    assert [request.tool_call_id for request in gateway.sql_requests] == [
        "call_initial",
        "call_rewrite",
    ]


@pytest.mark.asyncio
async def test_sql_repair_limit_finishes_partial_without_executing_a_third_rewrite() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_initial", "SELECT AVG(not.fully.paid) AS total FROM sales")
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_repair_1", "SELECT AVG(int.rate) AS total FROM sales")
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_repair_2", "SELECT AVG(log.annual.inc) AS total FROM sales")
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call_repair_3", "SELECT AVG(missing.rate) AS total FROM sales")
                ],
            ),
        ],
        [_answer("SQL 改写次数已达到上限。")],
    )
    gateway = FakeGateway(
        [
            _sql_guard_failure(audit_log_id="audit_initial"),
            _sql_guard_failure(audit_log_id="audit_repair_1", subject="AVG(int.rate)"),
            _sql_guard_failure(audit_log_id="audit_repair_2", subject="AVG(log.annual.inc)"),
        ]
    )

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "SQL_REPAIR_LIMIT_REACHED"
    assert [request.tool_call_id for request in gateway.sql_requests] == [
        "call_initial",
        "call_repair_1",
        "call_repair_2",
    ]
    assert [request.repaired_from_audit_id for request in gateway.sql_requests] == [
        None,
        "audit_initial",
        "audit_repair_1",
    ]


@pytest.mark.asyncio
async def test_internal_run_marker_in_final_answer_returns_one_safe_repair_to_agent() -> None:
    model = FakeModel(
        [AIMessage(content="初次结束"), AIMessage(content="补充后结束")],
        [_answer("R1 的结论", ["missing"]), _answer("有 sales 表。", ["schema"])],
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=FakeGateway(), plan=_answer_only_plan()),
    )

    assert state.outcome.answer == "有 sales 表。"
    assert len(model.agent_messages) == 2
    feedback = model.agent_messages[1][-1].content
    assert "内部运行标识" in feedback
    assert "R1 的结论" not in feedback


@pytest.mark.asyncio
async def test_inline_markdown_blocks_remain_a_safe_completed_answer() -> None:
    """标题或表格压成一行时交给 Markdown 渲染器处理，不触发模型重试。"""

    compact = (
        "### 字段映射 | 字段 | 来源 | | --- | --- | | purpose | 数据地图 | "
        "### 统计结果 - 记录数：1262"
    )
    model = FakeModel(
        [AIMessage(content="初次结束")],
        [_answer(compact)],
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            events=events,
            plan=_answer_only_plan(),
        ),
    )

    assert state.outcome.completion_kind == "completed"
    assert state.outcome.answer == compact
    assert len(model.agent_messages) == 1
    assert len(model.final_messages) == 1
    final_events = [
        event
        for event in events.events
        if event.type
        in {
            RunEventType.FINAL_ANSWER_REQUEST_STARTED,
            RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
            RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
            RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
        }
    ]
    assert [(event.type.value, event.payload["attempt"]) for event in final_events] == [
        ("final_answer.request.started", 1),
        ("final_answer.response.received", 1),
    ]


@pytest.mark.asyncio
async def test_tool_call_limit_finishes_as_partial_without_executing_over_budget_batch() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call(f"call_{index}", "SELECT 1") for index in range(9)],
            )
        ],
        [_answer("本次分析尚未执行查询，暂时无法给出完整结论。", ["schema"])],
    )
    gateway = FakeGateway()

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "TOOL_CALL_LIMIT_REACHED"
    assert state.outcome.answer == "当前尚未提交可验证结论，分析暂未完成。"
    assert gateway.sql_requests == []
    assert len(model.final_messages) == 1


@pytest.mark.asyncio
async def test_evidence_commit_does_not_consume_data_tool_call_budget() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(f"query_{index}", "SELECT amount AS total FROM sales")
                    for index in range(8)
                ],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
        ],
        [_answer("总额为 42。")],
    )
    model.plan = _materialized_plan("R1")
    gateway = FakeGateway([_sql_result(42) for _ in range(8)])

    state = await run_analysis_graph(_context(), GraphDependencies(model=model, gateway=gateway))

    assert len(gateway.sql_requests) == 8
    assert state.outcome.completion_kind == "completed"
    assert state.outcome.evidence_refs == ["audit_42", "table_42"]


@pytest.mark.asyncio
async def test_final_answer_fact_mismatch_finishes_as_safe_partial() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_for_fact_gate", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
        ],
        [
            _answer("| total |\n| --- |\n| 41 |"),
        ],
    )
    model.plan = _materialized_plan("R1")
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]), events=events),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "FINAL_ANSWER_FACT_MISMATCH"
    assert state.outcome.answer == "当前尚未提交可验证结论，分析暂未完成。"
    assert state.outcome.claim_audits[0].fact_validation_status == "mismatch"
    validation_events = [
        event
        for event in events.events
        if event.type is RunEventType.FINAL_ANSWER_VALIDATION_FAILED
    ]
    assert validation_events[-1].payload["failure_code"] == "FINAL_ANSWER_FACT_MISMATCH"


@pytest.mark.asyncio
async def test_final_answer_fact_gate_keeps_correct_markdown_completed() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("query_for_correct_fact", "SELECT amount AS total FROM sales")
                ],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
        ],
        [
            _answer("| total |\n| --- |\n| 42 |"),
        ],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    assert state.outcome.completion_kind == "completed"
    assert state.outcome.answer == "| total |\n| --- |\n| 42 |"


@pytest.mark.asyncio
async def test_duplicate_commit_for_one_requirement_does_not_duplicate_confirmed_fact() -> None:
    first_commit = _commit_call(
        "commit_first",
        values=[{"name": "total", "value": 42, "unit": None}],
    ).tool_calls[0]
    duplicate_commit = _commit_call(
        "commit_duplicate",
        values=[{"name": "total", "value": 42, "unit": None}],
    ).tool_calls[0]
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_for_commit", "SELECT amount AS total FROM sales")],
            ),
            AIMessage(content="", tool_calls=[first_commit, duplicate_commit]),
        ],
        [_answer("总额为 42。")],
    )
    model.plan = _materialized_plan("R1")

    state = await run_analysis_graph(
        _context(), GraphDependencies(model=model, gateway=FakeGateway([_sql_result(42)]))
    )

    final_snapshot = json.loads(model.final_messages[0][1].content)
    assert state.outcome.completion_kind == "completed"
    assert len(final_snapshot["confirmed_facts"]) == 1


@pytest.mark.asyncio
async def test_second_invalid_final_answer_finishes_as_partial_with_candidate_text() -> None:
    model = FakeModel(
        [AIMessage(content="初次结束"), AIMessage(content="修正后结束")],
        [_answer("R1 第一次候选", ["missing"]), _answer("R2 第二次候选", ["still_missing"])],
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=FakeGateway(), plan=_answer_only_plan()),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "FINAL_ANSWER_INVALID"
    assert state.outcome.answer == "本次分析未形成完整结论。"
    assert len(model.agent_messages) == 2
    assert len(model.final_messages) == 2


@pytest.mark.asyncio
async def test_second_invalid_final_answer_preserves_prior_partial_reason() -> None:
    """答案格式失败不能覆盖先前已经确定的分析未完成原因。"""

    model = FakeModel(
        [
            AIMessage(content="初次结束"),
            AIMessage(
                content="",
                tool_calls=[_tool_call(f"call_{index}", "SELECT 1") for index in range(9)],
            ),
        ],
        [_answer("R1 第一次候选"), _answer("R2 第二次候选")],
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=FakeGateway(), plan=_materialized_plan("R1")),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "TOOL_CALL_LIMIT_REACHED"
    assert state.outcome.answer == "本次分析未形成完整结论。"
    assert len(model.agent_messages) == 2
    assert len(model.final_messages) == 1


@pytest.mark.asyncio
async def test_model_global_deadline_finishes_as_partial_without_final_answer() -> None:
    model = FakeModel(
        [
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="模型调用超过本次分析总时限",
            )
        ],
        [],
    )

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(model=model, gateway=FakeGateway(), plan=_answer_only_plan()),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "RUN_TIMEOUT"
    assert state.outcome.answer == "本次分析未形成完整结论。"
    assert len(model.final_messages) == 0


@pytest.mark.asyncio
async def test_regular_agent_turn_timeout_finishes_as_partial_and_closes_turn() -> None:
    """普通 Agent 单轮超时不能留下只有 started 的悬挂回合。"""

    model = FakeModel(
        [
            AgentFailure(
                code=AgentErrorCode.ANALYSIS_AGENT_TURN_TIMEOUT,
                message="Agent 分析回合超过本阶段时限",
            )
        ],
        [],
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            events=events,
            plan=_answer_only_plan(),
        ),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "ANALYSIS_AGENT_TURN_TIMEOUT"
    turn_completed = [
        event for event in events.events if event.type.value == "agent.turn.completed"
    ]
    assert len(turn_completed) == 1
    assert turn_completed[0].payload["status"] == "failed"
    assert turn_completed[0].payload["failure_code"] == "ANALYSIS_AGENT_TURN_TIMEOUT"


@pytest.mark.asyncio
async def test_final_answer_stage_timeout_is_not_reported_as_run_timeout() -> None:
    """最终答案的独立短窗口不能被页面误显示为整轮总时限。"""

    model = FakeModel(
        [AIMessage(content="字段已足够")],
        [
            AgentFailure(
                code=AgentErrorCode.FINAL_ANSWER_TIMEOUT,
                message="最终答案模型请求超过本阶段时限",
            )
        ],
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            events=events,
            plan=_answer_only_plan(),
        ),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "FINAL_ANSWER_TIMEOUT"
    assert state.outcome.answer == "本次分析未形成完整结论。"
    assert state.outcome.evidence_refs == []
    assert "字段已足够" not in state.outcome.answer
    final_events = [
        event
        for event in events.events
        if event.type
        in {
            RunEventType.FINAL_ANSWER_REQUEST_STARTED,
            RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
        }
    ]
    assert [event.type.value for event in final_events] == [
        "final_answer.request.started",
        "final_answer.request.timed_out",
    ]
    assert final_events[1].payload == {
        "attempt": 1,
        "mode": "markdown",
        "elapsed_ms": final_events[1].payload["elapsed_ms"],
        "failure_code": AgentErrorCode.FINAL_ANSWER_TIMEOUT.value,
    }
    assert isinstance(final_events[1].payload["elapsed_ms"], int)


def test_final_answer_series_snapshot_caps_values_and_marks_truncation() -> None:
    values = tuple(AnalysisClaimValue(name="order_id", value=index) for index in range(72))

    projected = _project_final_answer_fact_values(values)

    assert [item["value"] for item in projected["values"]] == list(range(8))
    assert projected["series"] == [{"name": "order_id", "item_count": 72, "truncated": True}]


@pytest.mark.asyncio
async def test_context_only_schema_sources_keep_datalink_when_graph_exists() -> None:
    """Discovery 定稿常只写 schema；有图谱时仍开放 explore_datalink，不靠关键词路由。"""

    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_explore_datalink_call()]),
            AIMessage(content="已检索关系"),
        ],
        [_answer("订单通过 customer_id 关联客户。")],
    )
    gateway = FakeGateway()
    datalink = FakeDataLink(_semantic_datalink_result())
    context = _context().model_copy(update={"datalink_graph_version": "graph_1"})

    state = await run_analysis_graph(
        context,
        GraphDependencies(
            model=model,
            gateway=gateway,
            datalink=datalink,
            plan=_context_only_plan(),
        ),
    )

    assert gateway.sql_requests == []
    assert len(datalink.requests) == 1
    assert model.bound_tool_names == [["explore_datalink"], ["explore_datalink"]]
    assert state.outcome.completion_kind == "completed"
    snapshot = _final_answer_snapshot(model)
    assert snapshot["semantic_context"] is not None
    assert any(
        item.get("edge_type") == "foreign_key"
        for item in snapshot["semantic_context"]["relationships"]
    )


@pytest.mark.asyncio
async def test_context_only_datalink_explore_reaches_final_answer_snapshot() -> None:
    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_explore_datalink_call()]),
            AIMessage(content="已检索关系"),
        ],
        [_answer("订单通过 customer_id 关联客户。")],
    )
    gateway = FakeGateway()
    datalink = FakeDataLink(_semantic_datalink_result())
    context = _context().model_copy(update={"datalink_graph_version": "graph_1"})

    state = await run_analysis_graph(
        context,
        GraphDependencies(
            model=model,
            gateway=gateway,
            datalink=datalink,
            plan=_context_only_semantic_plan(),
        ),
    )

    assert gateway.sql_requests == []
    assert len(datalink.requests) == 1
    assert model.bound_tool_names == [["explore_datalink"], ["explore_datalink"]]
    assert state.outcome.answer == "订单通过 customer_id 关联客户。"
    assert state.outcome.completion_kind == "completed"
    snapshot = _final_answer_snapshot(model)
    semantic = snapshot["semantic_context"]
    assert semantic is not None
    assert semantic["join_paths"]
    assert any(item.get("edge_type") == "foreign_key" for item in semantic["relationships"])
    assert "private-customer-value" not in json.dumps(snapshot, ensure_ascii=False)


@pytest.mark.asyncio
async def test_context_only_datalink_unavailable_falls_back_to_schema_final_answer() -> None:
    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_explore_datalink_call()]),
            AIMessage(content="改用 Schema 说明关系"),
        ],
        [_answer("当前 Schema 可见 sales 表。")],
    )
    gateway = FakeGateway()
    datalink = FakeDataLink(
        AgentFailure(code=AgentErrorCode.DATALINK_UNAVAILABLE, message="DataLink 暂不可用")
    )
    context = _context().model_copy(update={"datalink_graph_version": "graph_1"})

    state = await run_analysis_graph(
        context,
        GraphDependencies(
            model=model,
            gateway=gateway,
            datalink=datalink,
            plan=_context_only_semantic_plan(),
        ),
    )

    assert gateway.sql_requests == []
    assert state.outcome.completion_kind == "completed"
    assert state.outcome.answer == "当前 Schema 可见 sales 表。"
    assert any(item.code is WarningCode.DATALINK_SCHEMA_ONLY for item in state.outcome.warnings)
    assert _final_answer_snapshot(model)["semantic_context"] is None


@pytest.mark.asyncio
async def test_final_answer_budget_exhaustion_closes_started_request_without_claims() -> None:
    model = FakeModel(
        [],
        [
            AgentFailure(
                code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                message="最终回答超出上下文预算",
            )
        ],
    )
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            events=events,
            plan=_context_only_plan(),
        ),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "CONTEXT_BUDGET_EXHAUSTED"
    assert state.outcome.answer == _SAFE_EVIDENCE_PARTIAL_ANSWER
    final_events = [
        event
        for event in events.events
        if event.type
        in {
            RunEventType.FINAL_ANSWER_REQUEST_STARTED,
            RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
            RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
            RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
        }
    ]
    assert [event.type.value for event in final_events] == [
        "final_answer.request.started",
        "final_answer.validation.failed",
    ]
    assert final_events[1].payload["failure_code"] == "CONTEXT_BUDGET_EXHAUSTED"
    assert final_events[1].payload["validation_stage"] == "budget"


@pytest.mark.asyncio
async def test_final_answer_budget_exhaustion_with_claims_uses_honest_partial() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("query_for_budget", "SELECT amount AS total FROM sales")],
            ),
            _commit_call(values=[{"name": "total", "value": 42, "unit": None}]),
        ],
        [
            AgentFailure(
                code=AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED,
                message="最终回答超出上下文预算",
            )
        ],
    )
    model.plan = _materialized_plan("R1")
    events = RecordingEvents()

    state = await run_analysis_graph(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway([_sql_result(42)]),
            events=events,
        ),
    )

    assert state.outcome.completion_kind == "partial"
    assert state.outcome.incomplete_reason == "CONTEXT_BUDGET_EXHAUSTED"
    assert state.outcome.answer == _CONTEXT_BUDGET_PARTIAL_ANSWER
    assert "尚未提交可验证结论" not in state.outcome.answer
    validation = next(
        event
        for event in events.events
        if event.type is RunEventType.FINAL_ANSWER_VALIDATION_FAILED
    )
    assert validation.payload["failure_code"] == "CONTEXT_BUDGET_EXHAUSTED"
    assert validation.payload["validation_stage"] == "budget"
