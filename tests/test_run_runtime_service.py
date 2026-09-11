from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    AnalysisOutcome,
    AnalysisPlanInvalidFailure,
    AnalysisPlanningDraft,
    ArtifactRef,
    ArtifactRegistration,
    ConversationContext,
    DiscoveryObservation,
    GraphState,
    OpeningValidationIssue,
    RecentUserTurnProjection,
    RunContext,
    RunOpeningDecision,
    SchemaContext,
)
from application.run_execution import RunExecutionContext
from application.run_runtime import (
    RunRuntimeError,
    RunRuntimeService,
    _GraphArtifactWriter,
)
from contracts.datasources import SchemaSummaryRead
from contracts.runs import ModelRuntimeSnapshot
from contracts.status import DataSourceStatus
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import (
    DataSourceModel,
    ModelProfileModel,
    RunEventModel,
    RunModel,
    SessionModel,
)
from runtime.run_event_pipeline import RunEventPipeline
from sqlalchemy import select


class FakeCancellation:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeDataLinkPort:
    async def explore(self, _request, _cancellation):
        raise AssertionError("preflight tests must not call DataLink")


class FakeSandbox:
    async def execute(self, _request, _cancellation):
        raise AssertionError("preflight tests must not call Docker")


class FakeOpeningModel:
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
        assert repair is False
        assert repair_issues == ()
        assert opening_context is not None
        return RunOpeningDecision(
            protocol_id="data-analysis",
            plan=AnalysisPlanningDraft(
                mode="ready",
                requirements=[
                    {
                        "description": "统计数值",
                        "acceptance_criteria": ["返回已验证数值"],
                        "fulfillment": {
                            "mode": "evidence",
                            "assertions": [
                                {
                                    "description": "读取数值",
                                    "claim_extractions": [
                                        {
                                            "mode": "scalar",
                                            "name": "value",
                                            "field": "value",
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


class FakeGeneralModel:
    def __init__(self) -> None:
        self.opening_calls = 0
        self.opening_contexts = []

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
        assert repair is False
        assert repair_issues == ()
        self.opening_calls += 1
        self.opening_contexts.append(opening_context)
        return RunOpeningDecision(protocol_id="general-task", answer="同比是同期比较。")


class FakeContextOnlyModel:
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
        assert repair is False
        assert repair_issues == ()
        assert opening_context is not None
        return RunOpeningDecision(
            protocol_id="data-analysis",
            plan=AnalysisPlanningDraft(
                mode="ready",
                requirements=[
                    {
                        "description": "说明当前字段",
                        "acceptance_criteria": ["只使用当前 Schema"],
                        "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                    }
                ],
            ),
        )


class FakeSemanticModel:
    def __init__(self) -> None:
        self.finalization_requests = []

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
        assert repair is False
        assert repair_issues == ()
        assert opening_context is not None
        return RunOpeningDecision(
            protocol_id="data-analysis",
            plan=AnalysisPlanningDraft(
                mode="needs_semantic_context",
                requirements=[
                    {
                        "description": "说明业务字段",
                        "acceptance_criteria": ["依据数据地图或 Schema"],
                        "fulfillment": {
                            "mode": "context_only",
                            "sources": ["semantic_context"],
                        },
                    }
                ],
                semantic_request={"query": "业务字段"},
            ),
        )

    async def finalize_analysis_plan(self, request, _cancellation):
        self.finalization_requests.append(request)
        return AnalysisPlanningDraft(
            mode="ready",
            requirements=[
                {
                    "description": "说明业务字段",
                    "acceptance_criteria": ["只依据当前 Schema"],
                    "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                }
            ],
        )


class FakeDiscoveryModel:
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
        assert repair is False
        assert repair_issues == ()
        assert opening_context is not None
        return RunOpeningDecision(
            protocol_id="data-analysis",
            plan=AnalysisPlanningDraft(
                mode="discovery",
                discovery_scope={
                    "tables": ["source"],
                    "columns": ["source.value"],
                },
                requirements=[
                    {
                        "description": "探索当前数据中的异常模式",
                        "acceptance_criteria": ["形成后续可验证目标"],
                        "fulfillment": {"mode": "context_only", "sources": ["schema"]},
                    }
                ],
            ),
        )


class FakeDiscoveryFinalizeModel(FakeDiscoveryModel):
    """Discovery 后只允许一次定稿，并返回可执行的正式证据计划。"""

    def __init__(self) -> None:
        self.finalization_requests = []

    async def finalize_analysis_plan(self, request, _cancellation):
        self.finalization_requests.append(request)
        return AnalysisPlanningDraft(
            mode="ready",
            requirements=[
                {
                    "description": "统计当前数值",
                    "acceptance_criteria": ["返回可核验的当前值"],
                    "fulfillment": {
                        "mode": "evidence",
                        "assertions": [
                            {
                                "description": "读取 value",
                                "source_tables": ["source"],
                                "result_columns": ["value"],
                                "claim_extractions": [
                                    {
                                        "mode": "scalar",
                                        "name": "value",
                                        "field": "value",
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        )


class FakeDiscoveryClarificationModel(FakeDiscoveryFinalizeModel):
    """无法从探索观察定义目标时，定稿必须转为澄清而非继续执行。"""

    async def finalize_analysis_plan(self, request, _cancellation):
        self.finalization_requests.append(request)
        return AnalysisPlanningDraft(
            mode="clarification",
            clarification={
                "question": "请补充要分析的范围。",
                "missing_items": ["data_scope"],
            },
        )


class FakeDiscoveryTimeoutModel(FakeDiscoveryFinalizeModel):
    """定稿窗口耗尽时返回稳定准备阶段错误。"""

    async def finalize_analysis_plan(self, request, _cancellation):
        self.finalization_requests.append(request)
        return AgentFailure(
            code="ANALYSIS_PREPARATION_BUDGET_EXHAUSTED",
            message="分析计划定稿超过阶段时限",
        )


class FakeDiscoveryInvalidJsonFinalizeModel(FakeDiscoveryFinalizeModel):
    """非法 JSON 工具调用必须留下计数，不得把参数原文写入事件。"""

    async def finalize_analysis_plan(self, request, _cancellation):
        self.finalization_requests.append(request)
        return AnalysisPlanInvalidFailure(
            message="模型没有返回合法的最终分析计划",
            validation_issues=[
                OpeningValidationIssue(
                    path="response.invalid_tool_calls",
                    error_type="json_invalid",
                    repair_reason="shape_invalid",
                    actual="valid=0 invalid=1 json_decode_error",
                    expected="唯一 finalize_analysis_plan 工具调用和合法 plan",
                    rule="定稿响应必须是无正文的单一 finalize_analysis_plan ToolCall",
                    action=(
                        "参数 JSON 不合法，请重新提交一次合法 JSON 对象的 "
                        "finalize_analysis_plan 调用"
                    ),
                )
            ],
        )


class FakeDiscoveryInvalidFinalizeModel(FakeDiscoveryFinalizeModel):
    """定稿字段错误必须留下安全的字段级诊断。"""

    async def finalize_analysis_plan(self, request, _cancellation):
        self.finalization_requests.append(request)
        return AnalysisPlanInvalidFailure(
            message="定稿计划字段不符合合同",
            validation_issues=[
                OpeningValidationIssue(
                    path="requirements[0].fulfillment.assertions[0].source_tables[0]",
                    error_type="unknown_table",
                    repair_reason="schema_reference_invalid",
                    actual="missing_table",
                    expected="当前冻结 Schema 中的物理引用",
                    rule="物理表必须存在",
                    action="改用当前 Schema 中的真实物理表",
                )
            ],
        )


class RecordingArtifactWriter:
    def __init__(self) -> None:
        self.requests: list[ArtifactRegistration] = []

    async def register_file(self, request, _cancellation) -> ArtifactRef:
        self.requests.append(request)
        return ArtifactRef(
            artifact_id="artifact_runtime",
            type=request.type,
            title=request.title,
            source_tool_call_id=request.source_tool_call_id,
        )


def _context(
    *,
    input_snapshot_ref: str | None = "snapshots/run_runtime_service/source.csv",
    input_filename: str | None = "source.csv",
) -> RunContext:
    return RunContext(
        run_id="run_runtime_service",
        session_id="session_runtime_service",
        datasource_id="datasource_runtime_service",
        model_profile_id="profile_runtime_service",
        model_name="fixed-model",
        schema_revision=1,
        datalink_graph_version=None,
        input_snapshot_ref=input_snapshot_ref,
        input_filename=input_filename,
        question="统计数值",
    )


async def _seed(
    settings,
    *,
    input_snapshot_ref: str | None = "snapshots/run_runtime_service/source.csv",
    datalink_graph_version: str | None = None,
) -> Path:
    source_path = settings.datasource_root / "datasource_runtime_service" / "source.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("value\n1\n", encoding="utf-8")
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            source = DataSourceModel(
                id="datasource_runtime_service",
                name="Runtime",
                description=None,
                type="csv",
                source_ref="datasource_runtime_service/source.csv",
                file_size=8,
                content_hash="hash",
                schema_cache_json={
                    "datasource_id": "datasource_runtime_service",
                    "dialect": "duckdb",
                    "tables": [
                        {
                            "name": "source",
                            "columns": [{"name": "value", "type": "BIGINT", "nullable": True}],
                            "row_count": 1,
                        }
                    ],
                },
                schema_revision=1,
                status=DataSourceStatus.READY.value,
            )
            profile = ModelProfileModel(
                id="profile_runtime_service",
                name="Fixed",
                provider="openai-compatible",
                model_name="fixed-model",
                base_url="https://model.example/v1",
                temperature=0,
                run_timeout_seconds=60,
                status="tested",
                is_active=False,
            )
            session = SessionModel(id="session_runtime_service", title="Runtime")
            db.add_all([source, profile, session])
            await db.flush()
            db.add(
                RunModel(
                    id="run_runtime_service",
                    session_id=session.id,
                    datasource_id=source.id,
                    question="统计数值",
                    status="queued",
                    model_profile_id=profile.id,
                    model_provider="openai-compatible",
                    model_name="fixed-model",
                    schema_revision=1,
                    datalink_graph_version=datalink_graph_version,
                    input_snapshot_ref=input_snapshot_ref,
                    run_timeout_seconds=60,
                    final_output_mode="json_schema",
                    model_capability_fingerprint="a" * 64,
                )
            )
            await db.commit()
    finally:
        await engine.dispose()
    return source_path


def _execution(source_path: Path | None) -> RunExecutionContext:
    has_snapshot = source_path is not None
    return RunExecutionContext(
        run_context=_context(
            input_snapshot_ref="snapshots/run_runtime_service/source.csv" if has_snapshot else None,
            input_filename="source.csv" if has_snapshot else None,
        ),
        model=ModelRuntimeSnapshot(
            profile_id="profile_runtime_service",
            provider="openai-compatible",
            model_name="fixed-model",
            base_url="https://model.example/v1",
            temperature=0,
            run_timeout_seconds=60,
            final_output_mode="json_schema",
            model_capability_fingerprint="a" * 64,
        ),
        api_key="fixed-api-key",
        sandbox_image="datapilot-analysis:0.1.0",
        input_snapshot_path=str(source_path) if source_path is not None else None,
    )


async def _with_service(
    settings,
    callback: Callable[[RunRuntimeService], Awaitable[None]],
    *,
    graph_runner=None,
    model_factory=None,
    datalink_factory=None,
    sandbox_factory=None,
) -> None:
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            service = RunRuntimeService(
                db=db,
                settings=settings,
                model_client_factory=model_factory,
                datalink_port_factory=datalink_factory or FakeDataLinkPort,
                sandbox_factory=sandbox_factory
                or (lambda _workspaces, _image, _deadline: FakeSandbox()),
                graph_runner=graph_runner,
                events=RunEventPipeline(db),
            )
            await callback(service)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_runtime_service_creates_run_workspace_from_fixed_input_snapshot(
    migrated_settings,
) -> None:
    source_path = await _seed(migrated_settings)
    observed: list[str] = []

    async def graph_runner(context, dependencies, _cancellation):
        workspace = dependencies.workspaces.get(dependencies.workspace_id)
        assert workspace.input_file is not None
        observed.append(workspace.input_file.read_text(encoding="utf-8"))
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            outcome=AnalysisOutcome(answer="完成"),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.answer == "完成"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: FakeOpeningModel(),
    )

    assert observed == ["value\n1\n"]
    assert list(migrated_settings.script_workspace_root.iterdir()) == []


@pytest.mark.asyncio
async def test_run_runtime_service_enters_graph_with_python_when_input_snapshot_is_missing(
    migrated_settings,
) -> None:
    await _seed(migrated_settings, input_snapshot_ref=None)
    observed: list[tuple[bool, bool, list[str]]] = []

    async def graph_runner(context, dependencies, _cancellation):
        assert dependencies.workspaces is not None
        assert dependencies.workspace_id is not None
        workspace = dependencies.workspaces.get(dependencies.workspace_id)
        observed.append(
            (
                dependencies.sandbox is not None,
                dependencies.artifacts is not None,
                workspace.input_file is None,
                sorted(path.name for path in workspace.input_dir.iterdir()),
            )
        )
        assert "run_python" not in dependencies.plan.constraints.forbidden_tools
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            outcome=AnalysisOutcome(answer="完成"),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(None))
        assert result.answer == "完成"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: FakeOpeningModel(),
    )

    assert observed == [(True, True, True, [])]
    assert list(migrated_settings.script_workspace_root.iterdir()) == []


@pytest.mark.asyncio
async def test_general_task_uses_one_opening_and_zero_analysis_resources(
    migrated_settings, monkeypatch
) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeGeneralModel()

    def forbidden_resource(*_args, **_kwargs):
        raise AssertionError("general task must not assemble analysis resources")

    monkeypatch.setattr("application.run_runtime.build_datasource_service", forbidden_resource)

    async def forbidden_graph(*_args, **_kwargs):
        raise AssertionError("general task must not enter Graph")

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.protocol_id == "general-task"
        assert result.answer == "同比是同期比较。"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=forbidden_graph,
        model_factory=lambda *_args: model,
        datalink_factory=forbidden_resource,
        sandbox_factory=forbidden_resource,
    )

    assert model.opening_calls == 1
    assert not migrated_settings.script_workspace_root.exists()

    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            event = await db.scalar(
                select(RunEventModel).where(
                    RunEventModel.run_id == "run_runtime_service",
                    RunEventModel.event_type == "run.preparation.completed",
                )
            )
            assert event is not None
            assert event.payload_json["phase"] == "run_opening"
            assert event.payload_json["opening_model_calls"] == 1
            assert event.payload_json["opening_repair_calls"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_general_task_opening_receives_frozen_session_context(migrated_settings) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeGeneralModel()
    context = ConversationContext(
        recent_user_turns=[RecentUserTurnProjection(content_text="以后回答请简短")],
    )

    async def exercise(service: RunRuntimeService) -> None:
        execution = _execution(source_path)
        execution = RunExecutionContext(
            run_context=execution.run_context.model_copy(update={"conversation_context": context}),
            model=execution.model,
            api_key=execution.api_key,
            sandbox_image=execution.sandbox_image,
            input_snapshot_path=execution.input_snapshot_path,
        )
        result = await service.execute(execution)
        assert result.protocol_id == "general-task"

    await _with_service(
        migrated_settings,
        exercise,
        model_factory=lambda *_args: model,
    )

    assert len(model.opening_contexts) == 1
    assert [item.content_text for item in model.opening_contexts[0].recent_user_turns] == [
        "以后回答请简短"
    ]


@pytest.mark.asyncio
async def test_context_only_plan_enters_graph_without_datalink_or_workspace(
    migrated_settings, monkeypatch
) -> None:
    source_path = await _seed(migrated_settings)
    observed_dependencies = []

    def forbidden_resource(*_args, **_kwargs):
        raise AssertionError("context-only plan must not assemble data tools")

    monkeypatch.setattr("application.run_runtime.build_datasource_service", forbidden_resource)

    async def graph_runner(context, dependencies, _cancellation):
        observed_dependencies.append(dependencies)
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            outcome=AnalysisOutcome(protocol_id="data-analysis", answer="当前字段为 value。"),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.answer == "当前字段为 value。"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: FakeContextOnlyModel(),
        datalink_factory=forbidden_resource,
        sandbox_factory=forbidden_resource,
    )

    assert len(observed_dependencies) == 1
    assert observed_dependencies[0].workspace_id is None
    assert observed_dependencies[0].datalink is None
    assert observed_dependencies[0].sandbox is None
    assert not migrated_settings.script_workspace_root.exists()


@pytest.mark.asyncio
async def test_context_only_plan_with_graph_injects_datalink_without_workspace(
    migrated_settings, monkeypatch
) -> None:
    source_path = await _seed(migrated_settings, datalink_graph_version="graph_1")
    observed_dependencies = []

    def forbidden_resource(*_args, **_kwargs):
        raise AssertionError("context-only plan must not assemble SQL or sandbox")

    monkeypatch.setattr("application.run_runtime.build_datasource_service", forbidden_resource)

    async def graph_runner(context, dependencies, _cancellation):
        observed_dependencies.append(dependencies)
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            outcome=AnalysisOutcome(protocol_id="data-analysis", answer="订单关联客户。"),
        )

    async def exercise(service: RunRuntimeService) -> None:
        execution = _execution(source_path)
        execution = RunExecutionContext(
            run_context=execution.run_context.model_copy(
                update={"datalink_graph_version": "graph_1"}
            ),
            model=execution.model,
            api_key=execution.api_key,
            sandbox_image=execution.sandbox_image,
            input_snapshot_path=execution.input_snapshot_path,
        )
        result = await service.execute(execution)
        assert result.answer == "订单关联客户。"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: FakeContextOnlyModel(),
        sandbox_factory=forbidden_resource,
    )

    assert len(observed_dependencies) == 1
    assert observed_dependencies[0].workspace_id is None
    assert observed_dependencies[0].datalink is not None
    assert observed_dependencies[0].sandbox is None
    assert not migrated_settings.script_workspace_root.exists()


@pytest.mark.asyncio
async def test_semantic_plan_without_graph_uses_one_finalization_and_schema_warning(
    migrated_settings, monkeypatch
) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeSemanticModel()
    observed_plans = []

    def forbidden_resource(*_args, **_kwargs):
        raise AssertionError("missing graph version must not create DataLink or workspace")

    monkeypatch.setattr("application.run_runtime.build_datasource_service", forbidden_resource)

    async def graph_runner(context, dependencies, _cancellation):
        observed_plans.append(dependencies.plan)
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            warnings=list(dependencies.plan.warnings),
            outcome=AnalysisOutcome(
                protocol_id="data-analysis",
                answer="当前仅能依据 Schema 说明字段。",
                warnings=list(dependencies.plan.warnings),
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.warnings[0].code.value == "DATALINK_SCHEMA_ONLY"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: model,
        datalink_factory=forbidden_resource,
        sandbox_factory=forbidden_resource,
    )

    assert len(model.finalization_requests) == 1
    assert model.finalization_requests[0].semantic_resolution is None
    assert model.finalization_requests[0].semantic_warning is not None
    assert len(observed_plans) == 1
    assert observed_plans[0].mode == "ready"


@pytest.mark.asyncio
async def test_discovery_finalization_ready_reenters_formal_graph(migrated_settings) -> None:
    """受限观察只用于定稿，正式 ready 计划必须重新走正式 Graph。"""

    source_path = await _seed(migrated_settings)
    model = FakeDiscoveryFinalizeModel()
    graph_calls = []

    async def graph_runner(context, dependencies, _cancellation):
        graph_calls.append(dependencies)
        schema_context = SchemaContext(
            datasource_id=context.datasource_id,
            schema_revision=context.schema_revision,
            schema_summary=SchemaSummaryRead(
                datasource_id=context.datasource_id,
                dialect="duckdb",
                tables=[],
            ),
        )
        if dependencies.plan.mode == "discovery":
            observation = DiscoveryObservation(
                tool_call_id="discovery_call",
                audit_log_id="discovery_audit",
                artifact_id="discovery_artifact",
                columns=["value"],
                row_count=1,
                rows_truncated=False,
            )
            return GraphState(
                run_context=context,
                schema_context=schema_context,
                discovery_observations=[observation],
                outcome=AnalysisOutcome(
                    protocol_id="data-analysis",
                    answer="已完成一次受限探索。",
                    completion_kind="partial",
                    incomplete_reason="DISCOVERY_OBSERVATION_ONLY",
                ),
            )
        assert dependencies.plan.mode == "ready"
        assert dependencies.workspace_id is not None
        assert dependencies.sandbox is not None
        return GraphState(
            run_context=context,
            schema_context=schema_context,
            outcome=AnalysisOutcome(
                protocol_id="data-analysis",
                answer="正式结论已完成。",
                evidence_refs=["evidence_formal"],
                completion_kind="completed",
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.answer == "正式结论已完成。"
        assert result.completion_kind == "completed"
        assert result.evidence_refs == ["evidence_formal"]

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: model,
    )

    assert [dependencies.plan.mode for dependencies in graph_calls] == ["discovery", "ready"]
    assert len(model.finalization_requests) == 1
    request = model.finalization_requests[0]
    assert request.initial_plan.mode == "discovery"
    assert [item.tool_call_id for item in request.discovery_observations] == ["discovery_call"]
    assert request.discovery_observations[0].model_dump(
        exclude={"tool_call_id", "audit_log_id", "artifact_id"}
    ) == {
        "columns": ["value"],
        "row_count": 1,
        "rows_truncated": False,
    }


@pytest.mark.asyncio
async def test_discovery_finalization_can_request_clarification(migrated_settings) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeDiscoveryClarificationModel()
    graph_calls = 0

    async def graph_runner(context, dependencies, _cancellation):
        nonlocal graph_calls
        graph_calls += 1
        assert dependencies.plan.mode == "discovery"
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            discovery_observations=[
                DiscoveryObservation(
                    tool_call_id="discovery_call",
                    audit_log_id="discovery_audit",
                    artifact_id="discovery_artifact",
                    columns=["value"],
                    row_count=1,
                )
            ],
            outcome=AnalysisOutcome(
                answer="探索完成。",
                completion_kind="partial",
                incomplete_reason="DISCOVERY_OBSERVATION_ONLY",
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.completion_kind == "clarification"
        assert result.answer == "请补充要分析的范围。"
        assert result.evidence_refs == []

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: model,
    )

    assert graph_calls == 1
    assert len(model.finalization_requests) == 1
    assert not migrated_settings.script_workspace_root.exists()


@pytest.mark.asyncio
async def test_discovery_finalization_timeout_stays_partial(migrated_settings) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeDiscoveryTimeoutModel()

    async def graph_runner(context, dependencies, _cancellation):
        assert dependencies.plan.mode == "discovery"
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            discovery_observations=[
                DiscoveryObservation(
                    tool_call_id="discovery_call",
                    audit_log_id="discovery_audit",
                    artifact_id="discovery_artifact",
                    columns=["value"],
                    row_count=1,
                )
            ],
            outcome=AnalysisOutcome(
                answer="探索完成。",
                completion_kind="partial",
                incomplete_reason="DISCOVERY_OBSERVATION_ONLY",
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.completion_kind == "partial"
        assert result.incomplete_reason == "DISCOVERY_FINALIZATION_TIMEOUT"
        assert result.evidence_refs == []
        assert result.claim_audits == []

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: model,
    )


@pytest.mark.asyncio
async def test_discovery_finalization_invalid_records_field_diagnostics(migrated_settings) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeDiscoveryInvalidFinalizeModel()

    async def graph_runner(context, dependencies, _cancellation):
        assert dependencies.plan.mode == "discovery"
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            discovery_observations=[
                DiscoveryObservation(
                    tool_call_id="discovery_call",
                    audit_log_id="discovery_audit",
                    artifact_id="discovery_artifact",
                    columns=["value"],
                    row_count=1,
                )
            ],
            outcome=AnalysisOutcome(
                answer="探索完成。",
                completion_kind="partial",
                incomplete_reason="DISCOVERY_OBSERVATION_ONLY",
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.completion_kind == "partial"
        assert result.incomplete_reason == "DISCOVERY_PLAN_INVALID"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: model,
    )

    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            events = (
                await db.scalars(
                    select(RunEventModel).where(
                        RunEventModel.run_id == "run_runtime_service",
                        RunEventModel.event_type == "run.preparation.completed",
                    )
                )
            ).all()
            event = next(
                item for item in events if item.payload_json.get("phase") == "analysis_plan"
            )
            assert event is not None
            assert event.payload_json["failure_code"] == AgentErrorCode.ANALYSIS_PLAN_INVALID.value
            assert event.payload_json["validation_issue_count"] == 1
            assert event.payload_json["validation_issue_types"] == "unknown_table"
            assert event.payload_json["validation_issue_reasons"] == "schema_reference_invalid"
            assert event.payload_json["validation_issue_paths"].startswith("requirements[0]")
    finally:
        await engine.dispose()

    assert len(model.finalization_requests) == 1


@pytest.mark.asyncio
async def test_discovery_finalization_invalid_json_records_tool_call_counts(
    migrated_settings,
) -> None:
    source_path = await _seed(migrated_settings)
    model = FakeDiscoveryInvalidJsonFinalizeModel()

    async def graph_runner(context, dependencies, _cancellation):
        assert dependencies.plan.mode == "discovery"
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            discovery_observations=[
                DiscoveryObservation(
                    tool_call_id="discovery_call",
                    audit_log_id="discovery_audit",
                    artifact_id="discovery_artifact",
                    columns=["value"],
                    row_count=1,
                )
            ],
            outcome=AnalysisOutcome(
                answer="探索完成。",
                completion_kind="partial",
                incomplete_reason="DISCOVERY_OBSERVATION_ONLY",
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.completion_kind == "partial"
        assert result.incomplete_reason == "DISCOVERY_PLAN_INVALID"

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=graph_runner,
        model_factory=lambda *_args: model,
    )

    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            events = (
                await db.scalars(
                    select(RunEventModel).where(
                        RunEventModel.run_id == "run_runtime_service",
                        RunEventModel.event_type == "run.preparation.completed",
                    )
                )
            ).all()
            event = next(
                item for item in events if item.payload_json.get("phase") == "analysis_plan"
            )
            payload = event.payload_json
            assert payload["failure_code"] == AgentErrorCode.ANALYSIS_PLAN_INVALID.value
            assert payload["validation_issue_count"] == 1
            assert payload["validation_issue_types"] == "json_invalid"
            assert payload["validation_issue_paths"] == "response.invalid_tool_calls"
            assert payload["validation_tool_call_count"] == 0
            assert payload["validation_invalid_tool_call_count"] == 1
            rendered = json.dumps(payload, ensure_ascii=False)
            assert "BROKEN_PLAN_ARGS_MUST_NOT_LEAK" not in rendered
            assert "SECRET_JSON_SNIPPET" not in rendered
            assert "JSONDecodeError" not in rendered
    finally:
        await engine.dispose()

    assert len(model.finalization_requests) == 1


@pytest.mark.asyncio
async def test_discovery_does_not_execute_or_promote_observations_to_claims(
    migrated_settings,
) -> None:
    source_path = await _seed(migrated_settings)
    observed_dependencies = []

    async def discovery_graph(context, dependencies, _cancellation):
        observed_dependencies.append(dependencies)
        return GraphState(
            run_context=context,
            schema_context=SchemaContext(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
                schema_summary=SchemaSummaryRead(
                    datasource_id=context.datasource_id,
                    dialect="duckdb",
                    tables=[],
                ),
            ),
            outcome=AnalysisOutcome(
                protocol_id="data-analysis",
                answer="已完成受限探索，尚未形成正式结论。",
                completion_kind="partial",
                incomplete_reason="DISCOVERY_OBSERVATION_ONLY",
            ),
        )

    async def exercise(service: RunRuntimeService) -> None:
        result = await service.execute(_execution(source_path))
        assert result.completion_kind == "partial"
        assert result.incomplete_reason == "DISCOVERY_OBSERVATION_ONLY"
        assert result.evidence_refs == []
        assert result.claim_audits == []

    await _with_service(
        migrated_settings,
        exercise,
        graph_runner=discovery_graph,
        model_factory=lambda *_args: FakeDiscoveryModel(),
    )

    assert len(observed_dependencies) == 1
    assert observed_dependencies[0].plan.mode == "discovery"
    assert observed_dependencies[0].workspace_id is None
    assert observed_dependencies[0].datalink is None
    assert observed_dependencies[0].sandbox is None


@pytest.mark.asyncio
async def test_run_runtime_service_rejects_fixed_context_mismatch_before_model(
    migrated_settings,
) -> None:
    source_path = await _seed(migrated_settings)
    model_calls = 0

    def model_factory(_snapshot, _api_key, _deadline):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("model must not be created")

    async def exercise(service: RunRuntimeService) -> None:
        execution = _execution(source_path)
        execution = RunExecutionContext(
            run_context=execution.run_context.model_copy(update={"schema_revision": 2}),
            model=execution.model,
            api_key=execution.api_key,
            sandbox_image=execution.sandbox_image,
            input_snapshot_path=execution.input_snapshot_path,
        )
        with pytest.raises(RunRuntimeError) as caught:
            await service.execute(execution)
        assert caught.value.failure.code.value == "DATA_GATEWAY_FAILED"

    await _with_service(
        migrated_settings,
        exercise,
        model_factory=model_factory,
        graph_runner=lambda *_args: None,
    )
    assert model_calls == 0


@pytest.mark.asyncio
async def test_graph_artifact_writer_keeps_tool_call_reference() -> None:
    inner = RecordingArtifactWriter()
    writer = _GraphArtifactWriter(inner)
    request = ArtifactRegistration(
        workspace_id="workspace_1",
        relative_path="outputs/summary.csv",
        type="file",
        title="摘要",
        purpose="结论证据",
        source_tool_call_id="tool_1",
    )

    artifact = await writer.register_file(request, FakeCancellation())

    assert inner.requests[0].source_tool_call_id == "tool_1"
    assert artifact.source_tool_call_id == "tool_1"
