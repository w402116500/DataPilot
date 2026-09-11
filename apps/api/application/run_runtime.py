"""选择 Run 协议并按固定上下文装配对应运行资源。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import cast

from agent_runtime.analysis_planning import materialize_analysis_plan
from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    AnalysisOutcome,
    AnalysisPlanFinalizationRequest,
    AnalysisPlanInvalidFailure,
    AnalysisPlanningDraft,
    AnalysisRequirement,
    AnalysisWarning,
    ArtifactRef,
    ArtifactRegistration,
    DataLinkExploreCommand,
    DataLinkExploreResponse,
    DataLinkSemanticContext,
    DiscoveryObservation,
    GraphState,
    OpeningValidationIssue,
    RunContext,
    RunOpeningDecision,
    SchemaLoadRequest,
    WarningCode,
)
from agent_runtime.conversation_context import datasource_identity, opening_context_projection
from agent_runtime.datalink_cache import CachingDataLinkPort
from agent_runtime.datalink_consumption import build_datalink_consumption
from agent_runtime.datalink_semantics import project_datalink_semantic_context
from agent_runtime.graph import GraphDependencies, GraphRunError, run_analysis_graph
from agent_runtime.observability import runtime_trace
from agent_runtime.ports import (
    ArtifactWriterPort,
    CancellationSignal,
    DataGatewayPort,
    DataLinkPort,
    ModelClientPort,
    RunEventPublisherPort,
    SandboxPort,
)
from agent_runtime.run_opening import open_run
from contracts.datasources import SchemaSummaryRead
from contracts.run_events import RunEventCreate, RunEventType
from contracts.runs import ModelRuntimeSnapshot
from contracts.status import DataSourceStatus
from contracts.validation import DataLinkConsumptionCreate
from metadata.models import DataSourceModel
from metadata.repositories import (
    ArtifactRepository,
    DataSourceRepository,
    RunRepository,
    SessionRepository,
)
from server.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession

from application.agent_ports import DataGatewayAgentPort, DataGatewaySchemaPort, DataLinkMcpPort
from application.datalink_client import DataLinkManagementPort
from application.datasource_factory import build_datasource_service
from application.docker_sandbox import DockerSandbox
from application.model_runtime import OpenAICompatibleModelClient, RunDeadline
from application.result_outputs import ResultOutputService
from application.run_execution import RunExecutionContext
from application.runtime_wait import RuntimeWaitCanceled, RuntimeWaitTimedOut, await_runtime_call
from application.script_workspace import ScriptWorkspaceManager

ModelClientFactory = Callable[[ModelRuntimeSnapshot, str, RunDeadline], ModelClientPort]
DataLinkPortFactory = Callable[[], DataLinkPort]
SandboxFactory = Callable[[ScriptWorkspaceManager, str, RunDeadline], SandboxPort]
GraphRunner = Callable[
    [RunContext, GraphDependencies, CancellationSignal | None],
    Awaitable[GraphState],
]


class RunRuntimeError(RuntimeError):
    """Run 预检、协议选择或 Graph 的分类失败。"""

    def __init__(self, failure: AgentFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class _NeverCanceled:
    """调用方未提供取消控制时使用的空实现。"""

    def is_cancelled(self) -> bool:
        return False


@dataclass(frozen=True)
class _ValidatedRun:
    """预检通过后才允许使用的固定数据源，密钥只来自私有执行上下文。"""

    datasource: DataSourceModel


class _GraphArtifactWriter:
    """把模型声明的 Python 输出交给结果产出服务登记。"""

    def __init__(self, writer: ArtifactWriterPort) -> None:
        self._writer = writer

    async def register_file(
        self,
        request: ArtifactRegistration,
        cancellation: CancellationSignal,
    ) -> ArtifactRef | AgentFailure:
        """结果产出服务负责路径、内容和 Artifact 索引校验。"""

        return await self._writer.register_file(request, cancellation)


class _RunDeadlineDataLinkPort:
    """把 DataLink 配置上限收紧到当前 Run 的真实剩余时间。"""

    def __init__(
        self,
        inner: DataLinkPort,
        *,
        deadline: RunDeadline,
        configured_timeout_seconds: float,
    ) -> None:
        self._inner = inner
        self._deadline = deadline
        self._configured_timeout_seconds = configured_timeout_seconds

    async def explore(
        self,
        request,
        cancellation: CancellationSignal,
    ):
        timeout_seconds = min(
            self._configured_timeout_seconds,
            self._deadline.remaining_seconds(),
        )
        if timeout_seconds <= 0:
            return AgentFailure(
                code=AgentErrorCode.DATALINK_UNAVAILABLE,
                message="DataLink 调用没有剩余 Run 时间",
            )
        try:
            return await await_runtime_call(
                self._inner.explore(request, cancellation),
                cancellation=cancellation,
                timeout_seconds=timeout_seconds,
            )
        except RuntimeWaitCanceled:
            return _cancelled_failure()
        except RuntimeWaitTimedOut:
            return AgentFailure(
                code=AgentErrorCode.DATALINK_UNAVAILABLE,
                message="DataLink 调用超过当前 Run 剩余时间",
            )


class RunRuntimeService:
    """使用已解析私有上下文选择协议，并延迟装配协议所需资源。"""

    def __init__(
        self,
        *,
        db: AsyncSession,
        settings: Settings,
        datalink_management_client: DataLinkManagementPort | None = None,
        model_client_factory: ModelClientFactory | None = None,
        datalink_port_factory: DataLinkPortFactory | None = None,
        sandbox_factory: SandboxFactory | None = None,
        graph_runner: GraphRunner | None = None,
        events: RunEventPublisherPort | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        if events is None:
            raise ValueError("Run Runtime requires a RunEventPipeline")
        self._datalink_management_client = datalink_management_client
        self._model_client_factory = model_client_factory or _build_model_client
        self._sandbox_factory = sandbox_factory or _sandbox_factory(settings)
        self._graph_runner = graph_runner or _run_graph
        self._events = events
        self._datalink_port_factory = datalink_port_factory or _datalink_port_factory(settings)

    async def execute(
        self,
        execution: RunExecutionContext,
        *,
        cancellation: CancellationSignal | None = None,
    ) -> AnalysisOutcome:
        """执行已解析的私有上下文，不从 Run 记录重新读取模型、密钥或快照。"""

        cancel = cancellation or _NeverCanceled()
        if cancel.is_cancelled():
            raise RunRuntimeError(_cancelled_failure())
        await self._validate_context(execution)
        if cancel.is_cancelled():
            raise RunRuntimeError(_cancelled_failure())

        context = execution.run_context
        deadline = RunDeadline(
            execution.model.run_timeout_seconds,
            limits=self._settings.runtime_limits,
        )
        model = self._model_client_factory(execution.model, execution.api_key, deadline)
        # Opening 只需要安全 Schema；完整 DataSourceService 会装配 SQL、Artifact
        # 和 DataLink 管理依赖，因此必须等协议确认后再创建。
        schema_gateway = DataGatewaySchemaPort(
            DataSourceRepository(self._db), execution.source_snapshot
        )
        schema = await schema_gateway.load_schema(
            SchemaLoadRequest(
                datasource_id=context.datasource_id,
                schema_revision=context.schema_revision,
            ),
            cancel,
        )
        if isinstance(schema, AgentFailure):
            raise RunRuntimeError(schema)
        opening_started_at = await self._start_preparation(context.run_id, "run_opening")
        opening_projection = opening_context_projection(
            context=context.conversation_context,
            question=context.question,
            identity=datasource_identity(
                name=context.datasource_display_name or "当前数据源",
                source_type=context.datasource_type or "unknown",
                schema=schema.schema_summary,
                schema_revision=context.schema_revision,
                description=context.datasource_description,
            ),
            schema=schema.schema_summary,
        )

        def validate_opening_plan(plan_draft: AnalysisPlanningDraft) -> AgentFailure | None:
            """Opening 首轮计划先过同一语义门禁，失败时交给唯一 repair。"""

            validation = materialize_analysis_plan(
                plan_draft,
                schema.schema_summary,
                limits=self._settings.agent_runtime_limits,
            )
            return validation if isinstance(validation, AgentFailure) else None

        async with runtime_trace(
            "datapilot.run.opening",
            run_id=context.run_id,
            session_id=context.session_id,
            phase="run_opening",
            run_type="llm",
            context_revision=context.context_projection_version,
            context_snapshot_hash=context.context_snapshot_hash,
            context_load_status=context.context_load_status,
            historical_summary_count=len(opening_projection.historical_summaries),
            recent_user_turn_count=len(opening_projection.recent_user_turns),
        ) as opening_span:
            opening = await open_run(
                model,
                question=context.question,
                schema=schema.schema_summary,
                opening_context=opening_projection,
                cancellation=cancel,
                plan_validator=validate_opening_plan,
            )
            opening_span.finish(
                error_code=(
                    opening.decision.code.value
                    if isinstance(opening.decision, AgentFailure)
                    else None
                ),
                protocol_id=(
                    opening.decision.protocol_id
                    if isinstance(opening.decision, RunOpeningDecision)
                    else None
                ),
                planning_mode=(
                    opening.decision.plan.mode
                    if isinstance(opening.decision, RunOpeningDecision)
                    and opening.decision.plan is not None
                    else None
                ),
                opening_model_calls=opening.opening_model_calls,
                opening_repair_calls=opening.opening_repair_calls,
                **_opening_diagnostic_metadata(opening.validation_issues),
                historical_summary_count=getattr(model, "last_opening_historical_summary_count", 0),
                historical_summary_omitted_count=getattr(
                    model, "last_opening_historical_summary_omitted_count", 0
                ),
                **(
                    model.last_request_budget.as_metadata()
                    if getattr(model, "last_request_budget", None) is not None
                    else {}
                ),
            )
        emitted_summary_count = getattr(model, "last_opening_historical_summary_count", None)
        if isinstance(emitted_summary_count, int):
            await self._record_historical_context_emission(
                context.run_id,
                context.historical_summary_ids[: max(0, emitted_summary_count)],
            )
        if isinstance(opening.decision, AgentFailure):
            await self._complete_preparation(
                context.run_id,
                "run_opening",
                opening_started_at,
                failure=opening.decision,
                opening_model_calls=opening.opening_model_calls,
                opening_repair_calls=opening.opening_repair_calls,
                validation_issues=opening.validation_issues,
            )
            raise RunRuntimeError(opening.decision)
        await self._complete_preparation(
            context.run_id,
            "run_opening",
            opening_started_at,
            opening_model_calls=opening.opening_model_calls,
            opening_repair_calls=opening.opening_repair_calls,
            validation_issues=opening.validation_issues,
        )
        decision = opening.decision
        await self._select_protocol(context.run_id, decision)
        if decision.protocol_id == "general-task":
            return AnalysisOutcome(
                protocol_id="general-task",
                answer=decision.answer or "",
                completion_kind="completed",
            )

        gateway: DataGatewayPort | None = None

        def build_agent_gateway() -> DataGatewayPort:
            """按需装配正式分析 Gateway，避免普通/Schema-only Run 初始化分析依赖。"""

            nonlocal gateway
            if gateway is None:
                datasources = build_datasource_service(
                    self._db,
                    self._settings,
                    datalink_client=self._datalink_management_client,
                )
                gateway = DataGatewayAgentPort(
                    datasources=datasources,
                    session_id=context.session_id,
                    schema_revision=context.schema_revision,
                    input_snapshot_path=execution.input_snapshot_path,
                    deadline=deadline,
                    source_snapshot=execution.source_snapshot,
                    source_access=execution.source_access,
                )
            return gateway

        def build_analysis_datalink() -> CachingDataLinkPort | None:
            """有图谱时才装配 MCP 端口；context_only 仍不开放 SQL/Python。"""

            if context.datalink_graph_version is None:
                return None
            return CachingDataLinkPort(
                _RunDeadlineDataLinkPort(
                    self._datalink_port_factory(),
                    deadline=deadline,
                    configured_timeout_seconds=self._settings.datalink_timeout_seconds,
                )
            )

        if decision.plan is None:
            raise RunRuntimeError(
                _failure(AgentErrorCode.ANALYSIS_PLAN_INVALID, "数据分析 Opening 缺少计划")
            )
        plan_draft = decision.plan
        semantic_context: DataLinkSemanticContext | None = None
        plan_warnings: tuple[AnalysisWarning, ...] = ()
        if plan_draft.mode == "needs_semantic_context":
            if opening.opening_repair_calls:
                raise RunRuntimeError(
                    _failure(
                        AgentErrorCode.ANALYSIS_PREPARATION_BUDGET_EXHAUSTED,
                        "Opening 已使用唯一修复机会，不能再次请求语义定稿",
                    )
                )
            plan_draft, semantic_context, plan_warnings = await self._finalize_semantic_plan(
                context=context,
                schema=schema.schema_summary,
                initial_plan=plan_draft,
                model=model,
                deadline=deadline,
                cancellation=cancel,
            )
        plan = materialize_analysis_plan(
            plan_draft,
            schema.schema_summary,
            semantic_context=semantic_context,
            warnings=plan_warnings,
            limits=self._settings.agent_runtime_limits,
        )
        if isinstance(plan, AgentFailure):
            raise RunRuntimeError(plan)
        if plan.consumes_pending and context.conversation_context.pending_clarification is None:
            raise RunRuntimeError(
                _failure(
                    AgentErrorCode.ANALYSIS_PLAN_INVALID,
                    "分析计划声明消费澄清事项，但当前 Session 没有待处理澄清",
                )
            )
        await self._publish_blocked_requirements(context.run_id, plan.requirements)
        if plan.mode == "clarification":
            clarification = plan.clarification
            if clarification is None:
                raise RunRuntimeError(
                    _failure(
                        AgentErrorCode.ANALYSIS_PLAN_INVALID,
                        "澄清计划缺少结构化澄清内容",
                    )
                )
            return AnalysisOutcome(
                protocol_id="data-analysis",
                answer=clarification.question,
                completion_kind="clarification",
                clarification=clarification,
                observations_performed=semantic_context is not None,
                consumes_pending=plan.consumes_pending,
            )
        if plan.mode == "needs_semantic_context":
            raise RunRuntimeError(
                _failure(AgentErrorCode.ANALYSIS_PLAN_INVALID, "语义定稿不得再次请求语义上下文")
            )

        if plan.mode == "discovery":
            analysis_gateway = build_agent_gateway()
            try:
                state = await self._run_agent_graph(
                    context,
                    GraphDependencies(
                        model=model,
                        gateway=analysis_gateway,
                        plan=plan,
                        events=self._events,
                        final_output_mode=execution.model.final_output_mode,
                        model_context=execution.model,
                        limits=self._settings.agent_runtime_limits,
                        record_datalink_consumption=self._record_datalink_consumption,
                    ),
                    cancel,
                )
            except GraphRunError as exc:
                raise RunRuntimeError(exc.failure) from exc
            if not state.discovery_observations:
                return state.outcome
            finalized = await self._finalize_discovery_plan(
                context=context,
                schema=schema.schema_summary,
                initial_plan=plan_draft,
                observations=state.discovery_observations,
                model=model,
                cancellation=cancel,
            )
            if isinstance(finalized, AgentFailure):
                if finalized.code is AgentErrorCode.RUN_CANCELED:
                    raise RunRuntimeError(finalized)
                if finalized.code is AgentErrorCode.CONTEXT_BUDGET_EXHAUSTED:
                    # Plan finalization has a single request and no repair
                    # budget. Preserve the explicit budget failure instead of
                    # relabeling it as a generic discovery contract error.
                    raise RunRuntimeError(finalized)
                reason = (
                    "DISCOVERY_FINALIZATION_TIMEOUT"
                    if finalized.code is AgentErrorCode.ANALYSIS_PREPARATION_BUDGET_EXHAUSTED
                    else "DISCOVERY_PLAN_INVALID"
                )
                return AnalysisOutcome(
                    protocol_id="data-analysis",
                    answer="已完成受限探索，但后续分析目标未能通过定稿校验。",
                    completion_kind="partial",
                    incomplete_reason=reason,
                    observations_performed=True,
                )
            plan = materialize_analysis_plan(
                finalized,
                schema.schema_summary,
                limits=self._settings.agent_runtime_limits,
            )
            if isinstance(plan, AgentFailure):
                return AnalysisOutcome(
                    protocol_id="data-analysis",
                    answer="已完成受限探索，但后续分析目标未能通过服务端校验。",
                    completion_kind="partial",
                    incomplete_reason="DISCOVERY_PLAN_INVALID",
                    observations_performed=True,
                )
            if plan.consumes_pending and context.conversation_context.pending_clarification is None:
                return AnalysisOutcome(
                    protocol_id="data-analysis",
                    answer="已完成受限探索，但定稿计划声明了不存在的待办澄清。",
                    completion_kind="partial",
                    incomplete_reason="DISCOVERY_PLAN_INVALID",
                    observations_performed=True,
                )
            await self._publish_blocked_requirements(context.run_id, plan.requirements)
            if plan.mode == "clarification":
                clarification = plan.clarification
                if clarification is None:
                    return AnalysisOutcome(
                        protocol_id="data-analysis",
                        answer="受限探索完成，但澄清计划缺少结构化澄清内容。",
                        completion_kind="partial",
                        incomplete_reason="DISCOVERY_PLAN_INVALID",
                        observations_performed=True,
                    )
                return AnalysisOutcome(
                    protocol_id="data-analysis",
                    answer=clarification.question,
                    completion_kind="clarification",
                    clarification=clarification,
                    observations_performed=True,
                    consumes_pending=plan.consumes_pending,
                )

        if plan.requirements and all(
            item.fulfillment_mode == "blocked" for item in plan.requirements
        ):
            return AnalysisOutcome(
                protocol_id="data-analysis",
                answer="当前分析目标受到数据、能力或用户约束限制，无法执行。",
                warnings=list(plan.warnings),
                completion_kind="partial",
                incomplete_reason="ANALYSIS_REQUIREMENTS_BLOCKED",
            )

        if not any(item.fulfillment_mode == "evidence" for item in plan.requirements):
            # Schema-only/context-only 计划不开放 SQL 执行；Graph 只会读取
            # load_schema，传入同一受限端口而不装配完整分析服务。
            # 有图谱时仍注入 DataLink MCP，否则 explore_datalink 只会得到
            # DATALINK_REQUEST_INVALID。
            try:
                state = await self._run_agent_graph(
                    context,
                    GraphDependencies(
                        model=model,
                        gateway=cast(DataGatewayPort, schema_gateway),
                        plan=plan,
                        datalink=build_analysis_datalink(),
                        events=self._events,
                        final_output_mode=execution.model.final_output_mode,
                        model_context=execution.model,
                        limits=self._settings.agent_runtime_limits,
                        record_datalink_consumption=self._record_datalink_consumption,
                    ),
                    cancel,
                )
            except GraphRunError as exc:
                raise RunRuntimeError(exc.failure) from exc
            return state.outcome

        analysis_gateway = build_agent_gateway()
        datalink = build_analysis_datalink()
        workspaces = ScriptWorkspaceManager(
            datasource_root=self._settings.datasource_root,
            workspace_root=self._settings.script_workspace_root,
            runtime_trace_root=self._settings.runtime_trace_root,
        )
        try:
            workspace = await workspaces.create(
                run_id=context.run_id,
                input_snapshot_path=execution.input_snapshot_path,
            )
        except Exception as exc:
            raise RunRuntimeError(
                _failure(AgentErrorCode.SANDBOX_REJECTED, "分析工作区无法安全创建")
            ) from exc

        try:
            result_outputs = ResultOutputService(
                workspaces=workspaces,
                repository=ArtifactRepository(self._db),
                artifact_root=self._settings.artifact_root,
                run_id=context.run_id,
                session_id=context.session_id,
            )
            dependencies = GraphDependencies(
                model=model,
                gateway=analysis_gateway,
                plan=plan,
                datalink=datalink,
                events=self._events,
                workspace_id=workspace.workspace_id,
                workspaces=workspaces,
                sandbox=self._sandbox_factory(workspaces, execution.sandbox_image, deadline),
                artifacts=_GraphArtifactWriter(result_outputs),
                final_output_mode=execution.model.final_output_mode,
                model_context=execution.model,
                limits=self._settings.agent_runtime_limits,
                record_datalink_consumption=self._record_datalink_consumption,
            )
            state = await self._run_agent_graph(context, dependencies, cancel)
        except GraphRunError as exc:
            raise RunRuntimeError(exc.failure) from exc
        finally:
            await workspaces.cleanup(workspace.workspace_id)

        return state.outcome

    async def _record_historical_context_emission(
        self, run_id: str, emitted_summary_ids: Sequence[str]
    ) -> None:
        """记录已序列化进 Opening 投影的摘要 ID 数量，而不是按 topic 猜测。"""

        run = await RunRepository(self._db).get(run_id)
        if run is None:
            return
        count = min(len(set(emitted_summary_ids)), 3)
        run.historical_context_injected = count > 0
        run.historical_summary_count = count
        await self._db.commit()

    async def _finalize_discovery_plan(
        self,
        *,
        context: RunContext,
        schema: SchemaSummaryRead,
        initial_plan: AnalysisPlanningDraft,
        observations: Sequence[DiscoveryObservation],
        model: ModelClientPort,
        cancellation: CancellationSignal,
    ) -> AnalysisPlanningDraft | AgentFailure:
        """将安全 Discovery 观察定稿为 ready 或 clarification。"""

        plan_started_at = await self._start_preparation(context.run_id, "analysis_plan")
        capabilities = [
            "run_sql_readonly",
            "run_python",
            "artifact_writer",
            "commit_analysis_claims",
        ]
        if context.datalink_graph_version is not None:
            capabilities.append("explore_datalink")
        async with runtime_trace(
            "datapilot.run.plan_finalization",
            run_id=context.run_id,
            session_id=context.session_id,
            phase="analysis_plan",
            run_type="llm",
            protocol_id="data-analysis",
            planning_mode="discovery",
            context_revision=context.context_projection_version,
            context_snapshot_hash=context.context_snapshot_hash,
        ) as plan_span:
            finalized = await model.finalize_analysis_plan(
                AnalysisPlanFinalizationRequest(
                    question=context.question,
                    physical_schema=schema,
                    datasource_revision=context.schema_revision,
                    initial_plan=initial_plan,
                    discovery_observations=list(observations),
                    available_capabilities=capabilities,
                ),
                cancellation,
            )
            plan_span.finish(
                error_code=(finalized.code.value if isinstance(finalized, AgentFailure) else None),
                protocol_id="data-analysis",
                planning_mode=(
                    finalized.mode if isinstance(finalized, AnalysisPlanningDraft) else "discovery"
                ),
                plan_model_calls=1,
                **(
                    _opening_diagnostic_metadata(finalized.validation_issues)
                    if isinstance(finalized, AnalysisPlanInvalidFailure)
                    else {}
                ),
                **(
                    model.last_request_budget.as_metadata()
                    if getattr(model, "last_request_budget", None) is not None
                    else {}
                ),
            )
        if isinstance(finalized, AgentFailure):
            await self._complete_preparation(
                context.run_id,
                "analysis_plan",
                plan_started_at,
                failure=finalized,
                validation_issues=(
                    finalized.validation_issues
                    if isinstance(finalized, AnalysisPlanInvalidFailure)
                    else ()
                ),
            )
            return finalized
        if finalized.mode not in {"ready", "clarification"}:
            failure = _failure(
                AgentErrorCode.ANALYSIS_PLAN_INVALID,
                "Discovery 定稿返回了不允许的计划模式",
            )
            await self._complete_preparation(
                context.run_id,
                "analysis_plan",
                plan_started_at,
                failure=failure,
            )
            return failure
        await self._complete_preparation(context.run_id, "analysis_plan", plan_started_at)
        return finalized

    async def _finalize_semantic_plan(
        self,
        *,
        context: RunContext,
        schema: SchemaSummaryRead,
        initial_plan: AnalysisPlanningDraft,
        model: ModelClientPort,
        deadline: RunDeadline,
        cancellation: CancellationSignal,
    ) -> tuple[AnalysisPlanningDraft, DataLinkSemanticContext | None, tuple[AnalysisWarning, ...]]:
        """执行至多一次语义检索和一次计划定稿。"""

        semantic_context: DataLinkSemanticContext | None = None
        warning: AnalysisWarning | None = None
        semantic_request = initial_plan.semantic_request
        if semantic_request is None:
            raise RunRuntimeError(
                _failure(AgentErrorCode.ANALYSIS_PLAN_INVALID, "语义计划缺少受限检索请求")
            )
        semantic_started_at = await self._start_preparation(context.run_id, "semantic_context")
        explore_result: DataLinkExploreResponse | AgentFailure | None = None
        if context.datalink_graph_version is None:
            warning = _semantic_unavailable_warning("当前 Run 没有可用的数据地图版本")
        else:
            datalink = CachingDataLinkPort(
                _RunDeadlineDataLinkPort(
                    self._datalink_port_factory(),
                    deadline=deadline,
                    configured_timeout_seconds=self._settings.datalink_timeout_seconds,
                )
            )
            explore_result = await datalink.explore(
                DataLinkExploreCommand(
                    datasource_id=context.datasource_id,
                    schema_revision=context.schema_revision,
                    graph_version=context.datalink_graph_version,
                    query=semantic_request.query,
                    focus=semantic_request.focus,
                    max_nodes=semantic_request.max_nodes,
                ),
                cancellation,
            )
            if isinstance(explore_result, AgentFailure):
                if explore_result.code is AgentErrorCode.DATALINK_UNAVAILABLE:
                    warning = _semantic_unavailable_warning(
                        "DataLink 暂不可用，计划仅依据 Schema 定稿"
                    )
                else:
                    await self._record_prepare_consumption(
                        context, semantic_request, explore_result
                    )
                    await self._complete_preparation(
                        context.run_id,
                        "semantic_context",
                        semantic_started_at,
                        failure=explore_result,
                    )
                    raise RunRuntimeError(explore_result)
            else:
                semantic_context = project_datalink_semantic_context(explore_result)
                if not semantic_context.fields:
                    semantic_context = None
                    warning = AnalysisWarning(
                        code=WarningCode.DATALINK_NO_MATCH_SCHEMA_ONLY,
                        message="数据地图未返回匹配字段，计划仅依据 Schema 定稿",
                    )
        await self._record_prepare_consumption(context, semantic_request, explore_result)
        await self._complete_preparation(
            context.run_id,
            "semantic_context",
            semantic_started_at,
        )

        capabilities = [
            "run_sql_readonly",
            "run_python",
            "artifact_writer",
            "commit_analysis_claims",
        ]
        if context.datalink_graph_version is not None:
            capabilities.append("explore_datalink")
        plan_started_at = await self._start_preparation(context.run_id, "analysis_plan")
        async with runtime_trace(
            "datapilot.run.plan_finalization",
            run_id=context.run_id,
            session_id=context.session_id,
            phase="analysis_plan",
            run_type="llm",
            protocol_id="data-analysis",
            planning_mode=initial_plan.mode,
            context_revision=context.context_projection_version,
            context_snapshot_hash=context.context_snapshot_hash,
        ) as plan_span:
            finalized = await model.finalize_analysis_plan(
                AnalysisPlanFinalizationRequest(
                    question=context.question,
                    physical_schema=schema,
                    datasource_revision=context.schema_revision,
                    initial_plan=initial_plan,
                    semantic_resolution=semantic_context,
                    semantic_warning=warning,
                    available_capabilities=capabilities,
                ),
                cancellation,
            )
            plan_span.finish(
                error_code=(finalized.code.value if isinstance(finalized, AgentFailure) else None),
                protocol_id="data-analysis",
                planning_mode=(
                    finalized.mode
                    if isinstance(finalized, AnalysisPlanningDraft)
                    else initial_plan.mode
                ),
                plan_model_calls=1,
                **(
                    model.last_request_budget.as_metadata()
                    if getattr(model, "last_request_budget", None) is not None
                    else {}
                ),
            )
        if isinstance(finalized, AgentFailure):
            await self._complete_preparation(
                context.run_id,
                "analysis_plan",
                plan_started_at,
                failure=finalized,
            )
            raise RunRuntimeError(finalized)
        if finalized.mode not in {"ready", "clarification"}:
            invalid = _failure(
                AgentErrorCode.ANALYSIS_PLAN_INVALID,
                "语义定稿返回了不允许的计划模式",
            )
            await self._complete_preparation(
                context.run_id,
                "analysis_plan",
                plan_started_at,
                failure=invalid,
            )
            raise RunRuntimeError(invalid)
        await self._complete_preparation(
            context.run_id,
            "analysis_plan",
            plan_started_at,
        )
        return finalized, semantic_context, ((warning,) if warning is not None else ())

    async def _run_agent_graph(
        self,
        context: RunContext,
        dependencies: GraphDependencies,
        cancellation: CancellationSignal,
    ) -> GraphState:
        assertion_count = sum(
            len(requirement.assertions) for requirement in dependencies.plan.requirements
        )
        limits = dependencies.limits
        async with runtime_trace(
            "datapilot.run.agent",
            run_id=context.run_id,
            session_id=context.session_id,
            phase="agent",
            protocol_id="data-analysis",
            planning_mode=dependencies.plan.mode,
            context_revision=context.context_projection_version,
            context_snapshot_hash=context.context_snapshot_hash,
            plan_requirement_count=len(dependencies.plan.requirements),
            plan_assertion_count=assertion_count,
            context_window_tokens=(
                dependencies.model_context.context_window_tokens
                if dependencies.model_context is not None
                else None
            ),
            max_turns=limits.max_turns,
            max_data_tool_calls=limits.max_data_tool_calls,
            max_assertions_per_requirement=limits.max_assertions_per_requirement,
            max_total_assertions=limits.max_total_assertions,
            max_query_attempts_per_assertion=limits.max_query_attempts_per_assertion,
            max_assertion_failures=limits.max_assertion_failures,
        ) as agent_span:
            try:
                state = await self._graph_runner(context, dependencies, cancellation)
            except GraphRunError as exc:
                agent_span.finish(
                    error_code=exc.failure.code.value,
                    protocol_id="data-analysis",
                    planning_mode=dependencies.plan.mode,
                )
                raise
            agent_span.finish(
                protocol_id="data-analysis",
                planning_mode=dependencies.plan.mode,
                completion_kind=(
                    state.outcome.completion_kind if state.outcome is not None else None
                ),
                context_retry_count=state.context_retry_count,
                context_compaction_count=state.context_compaction_count,
                working_set_count=state.working_set_count,
                blocked_assertion_count=state.blocked_assertion_count,
                **(
                    dependencies.model.last_request_budget.as_metadata()
                    if getattr(dependencies.model, "last_request_budget", None) is not None
                    else {}
                ),
            )
            return state

    async def _record_datalink_consumption(self, payload: DataLinkConsumptionCreate) -> None:
        recorder = getattr(self._events, "record_datalink_consumption", None)
        if recorder is None:
            return
        await recorder(payload)

    async def _record_prepare_consumption(
        self,
        context: RunContext,
        semantic_request,
        result: DataLinkExploreResponse | AgentFailure | None,
    ) -> None:
        await self._record_datalink_consumption(
            build_datalink_consumption(
                run_id=context.run_id,
                stage="prepare",
                query=semantic_request.query,
                focus=semantic_request.focus,
                max_nodes=semantic_request.max_nodes,
                schema_revision=context.schema_revision,
                graph_version=context.datalink_graph_version,
                result=result,
            )
        )

    async def _start_preparation(self, run_id: str, phase: str) -> float:
        failure = await self._events.publish(
            RunEventCreate(
                run_id=run_id,
                type=RunEventType.RUN_PREPARATION_STARTED,
                payload={"phase": phase},
            )
        )
        if failure is not None:
            raise RunRuntimeError(failure)
        return time.monotonic()

    async def _publish_blocked_requirements(
        self, run_id: str, requirements: Sequence[AnalysisRequirement]
    ) -> None:
        for requirement in requirements:
            if requirement.fulfillment_mode != "blocked" or requirement.block_reason is None:
                continue
            failure = await self._events.publish(
                RunEventCreate(
                    run_id=run_id,
                    type=RunEventType.ANALYSIS_REQUIREMENT_BLOCKED,
                    payload={
                        "requirement_id": requirement.id,
                        "reason_code": requirement.block_reason,
                    },
                )
            )
            if failure is not None:
                raise RunRuntimeError(failure)

    async def _complete_preparation(
        self,
        run_id: str,
        phase: str,
        started_at: float,
        *,
        failure: AgentFailure | None = None,
        opening_model_calls: int | None = None,
        opening_repair_calls: int | None = None,
        validation_issues: Sequence[OpeningValidationIssue] = (),
    ) -> None:
        payload: dict[str, object] = {
            "phase": phase,
            "elapsed_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            "status": _preparation_status(failure),
        }
        if failure is not None:
            payload["failure_code"] = failure.code.value
        if opening_model_calls is not None:
            payload["opening_model_calls"] = opening_model_calls
        if opening_repair_calls is not None:
            payload["opening_repair_calls"] = opening_repair_calls
        payload.update(_opening_diagnostic_metadata(validation_issues))
        persistence_failure = await self._events.publish(
            RunEventCreate(
                run_id=run_id,
                type=RunEventType.RUN_PREPARATION_COMPLETED,
                payload=payload,
            )
        )
        if persistence_failure is not None:
            raise RunRuntimeError(persistence_failure)

    async def _select_protocol(
        self,
        run_id: str,
        decision: RunOpeningDecision,
    ) -> None:
        """在 Run 字段和唯一协议事件之间保持同一事务。"""

        async with self._events.transaction(run_id):
            result = await RunRepository(self._db).select_protocol_if_unset(
                run_id, decision.protocol_id
            )
            if result == "conflict":
                raise RunRuntimeError(
                    _failure(
                        AgentErrorCode.RUN_PROTOCOL_CONFLICT,
                        "Run 协议已经被其他 Opening 选择",
                    )
                )
            if result == "missing":
                raise RunRuntimeError(
                    _failure(AgentErrorCode.DATA_GATEWAY_FAILED, "协议选择时 Run 不存在")
                )
            if result == "idempotent":
                return
            payload: dict[str, object] = {
                "protocol_id": decision.protocol_id,
                "selection_mode": "opening",
            }
            if decision.plan is not None:
                payload["planning_mode"] = decision.plan.mode
            await self._events.append(
                RunEventCreate(
                    run_id=run_id,
                    type=RunEventType.RUN_PROTOCOL_SELECTED,
                    payload=payload,
                )
            )
            await self._db.commit()
        await self._events.notify_committed(run_id)

    async def _validate_context(self, execution: RunExecutionContext) -> _ValidatedRun:
        """在模型、MCP、SQL 或工作区创建前核对固定的 Run 与数据源身份。"""

        context = execution.run_context
        session = await SessionRepository(self._db).get(context.session_id)
        run = await RunRepository(self._db).get(context.run_id)
        source = await DataSourceRepository(self._db).get(context.datasource_id)
        if session is None or run is None:
            raise RunRuntimeError(
                _failure(AgentErrorCode.DATA_GATEWAY_FAILED, "Run 或 Session 不存在")
            )
        if (
            run.session_id != context.session_id
            or run.datasource_id != context.datasource_id
            # ModelProfile 删除会让历史 Run 的外键按 SET NULL 清空；本次 Run 已经
            # 持有请求时解析的模型参数与密钥，不能因此中断正在执行的分析。
            or (
                run.model_profile_id is not None
                and run.model_profile_id != context.model_profile_id
            )
            or run.model_name != context.model_name
            or run.schema_revision != context.schema_revision
            or run.connection_revision != context.connection_revision
            or run.datalink_graph_version != context.datalink_graph_version
            or run.input_snapshot_ref != context.input_snapshot_ref
            or run.question != context.question
        ):
            raise RunRuntimeError(
                _failure(AgentErrorCode.DATA_GATEWAY_FAILED, "Run 固定上下文与请求不一致")
            )
        if (
            execution.model.profile_id != context.model_profile_id
            or execution.model.model_name != context.model_name
            or run.model_provider != execution.model.provider
            or run.model_name != execution.model.model_name
            or run.run_timeout_seconds != execution.model.run_timeout_seconds
            or run.final_output_mode != execution.model.final_output_mode
            or run.model_capability_fingerprint != execution.model.model_capability_fingerprint
        ):
            raise RunRuntimeError(
                _failure(AgentErrorCode.MODEL_REQUEST_FAILED, "Run 固定模型配置与请求不一致")
            )
        if execution.source_snapshot is not None:
            if (
                source is None
                or source.status in {DataSourceStatus.DELETING, DataSourceStatus.DELETED}
                or execution.source_snapshot.id != context.datasource_id
                or execution.source_snapshot.schema_revision != context.schema_revision
                or execution.source_snapshot.connection_revision != context.connection_revision
            ):
                raise RunRuntimeError(
                    _failure(AgentErrorCode.DATA_GATEWAY_FAILED, "Run 固定上下文与数据源不一致")
                )
        elif (
            source is None
            or source.status
            not in {
                DataSourceStatus.SCHEMA_READY.value,
                DataSourceStatus.READY.value,
            }
            or source.schema_revision != context.schema_revision
            or source.schema_cache_json is None
            or source.source_ref is None
        ):
            raise RunRuntimeError(
                _failure(AgentErrorCode.DATA_GATEWAY_FAILED, "数据源或 Schema 与当前 Run 不一致")
            )
        return _ValidatedRun(datasource=source)


def _build_model_client(
    snapshot: ModelRuntimeSnapshot,
    api_key: str,
    deadline: RunDeadline,
) -> ModelClientPort:
    """只按 Run 快照创建客户端，避免读取已被更新的 Profile 配置。"""

    return OpenAICompatibleModelClient(
        model_name=snapshot.model_name,
        base_url=str(snapshot.base_url),
        api_key=api_key,
        temperature=snapshot.temperature,
        deadline=deadline,
        model_context=snapshot,
    )


def _datalink_port_factory(settings: Settings) -> DataLinkPortFactory:
    """固定当前 Settings 的 MCP 地址，避免执行中重新读取环境配置。"""

    def build() -> DataLinkPort:
        return DataLinkMcpPort(
            endpoint=settings.datalink_mcp_url,
            timeout_seconds=settings.datalink_timeout_seconds,
        )

    return build


def _sandbox_factory(settings: Settings) -> SandboxFactory:
    """按 Run 已固定的镜像创建 Sandbox，避免执行中重读环境配置。"""

    def build(workspaces: ScriptWorkspaceManager, image: str, deadline: RunDeadline) -> SandboxPort:
        return DockerSandbox(
            workspaces=workspaces,
            image=image,
            timeout_seconds=settings.python_sandbox_timeout_seconds,
            timeout_provider=deadline.remaining_seconds,
        )

    return build


async def _run_graph(
    context: RunContext,
    dependencies: GraphDependencies,
    cancellation: CancellationSignal | None,
) -> GraphState:
    """把默认 Graph 函数适配为可在测试中替换的内部调用形状。"""

    return await run_analysis_graph(context, dependencies, cancellation=cancellation)


def _cancelled_failure() -> AgentFailure:
    return _failure(AgentErrorCode.RUN_CANCELED, "分析已取消")


def _failure(code: AgentErrorCode, message: str) -> AgentFailure:
    return AgentFailure(code=code, message=message)


def _semantic_unavailable_warning(message: str) -> AnalysisWarning:
    return AnalysisWarning(code=WarningCode.DATALINK_SCHEMA_ONLY, message=message)


def _preparation_status(failure: AgentFailure | None) -> str:
    if failure is None:
        return "completed"
    if failure.code is AgentErrorCode.RUN_CANCELED:
        return "cancelled"
    if failure.code in {
        AgentErrorCode.RUN_OPENING_TIMEOUT,
        AgentErrorCode.ANALYSIS_PREPARATION_BUDGET_EXHAUSTED,
    }:
        return "timed_out"
    return "failed"


def _tool_call_counts_from_actual(actual: str) -> tuple[int, int] | None:
    valid: int | None = None
    invalid: int | None = None
    for part in actual.split():
        if part.startswith("valid=") and part[6:].isdigit():
            valid = int(part[6:])
        elif part.startswith("invalid=") and part[8:].isdigit():
            invalid = int(part[8:])
    if valid is None or invalid is None:
        return None
    return valid, invalid


def _opening_diagnostic_metadata(
    issues: Sequence[OpeningValidationIssue] | Sequence[object],
) -> dict[str, object]:
    """只投影 Opening 修复所需的诊断索引，不泄露模型参数或原始值。"""

    safe_issues = [item for item in issues if isinstance(item, OpeningValidationIssue)][:12]
    if not safe_issues:
        return {}
    metadata: dict[str, object] = {
        "validation_issue_count": len(safe_issues),
        "validation_issue_types": ",".join(dict.fromkeys(item.error_type for item in safe_issues)),
        "validation_issue_reasons": ",".join(
            dict.fromkeys(item.repair_reason for item in safe_issues)
        ),
        "validation_issue_paths": ",".join(dict.fromkeys(item.path for item in safe_issues)),
    }
    for item in safe_issues:
        counts = _tool_call_counts_from_actual(item.actual)
        if counts is None:
            continue
        metadata["validation_tool_call_count"] = counts[0]
        metadata["validation_invalid_tool_call_count"] = counts[1]
        break
    return metadata
