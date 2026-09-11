"""Agent Runtime 对外层能力的最小 Protocol 边界。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from contracts.datasources import SchemaSummaryRead
from contracts.model_profiles import FinalOutputMode
from contracts.run_events import RunEventCreate
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from agent_runtime.contracts import (
    AgentFailure,
    AnalysisPlanFinalizationRequest,
    AnalysisPlanningDraft,
    ArtifactRef,
    ArtifactRegistration,
    DataLinkExploreCommand,
    DataLinkExploreResponse,
    FinalMarkdownPayload,
    OpeningContextProjection,
    OpeningValidationIssue,
    RunOpeningDecision,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SchemaContext,
    SchemaLoadRequest,
    SqlExecutionFailure,
    SqlExecutionRequest,
    SqlExecutionResult,
)


class CancellationSignal(Protocol):
    """Runtime 只需要查询取消状态，具体 Event 或请求对象留在外层。"""

    def is_cancelled(self) -> bool: ...


class ModelClientPort(Protocol):
    """向模型发送标准消息；连接配置和密钥不穿过 Runtime 边界。"""

    async def open_run(
        self,
        question: str,
        schema: SchemaSummaryRead,
        cancellation: CancellationSignal,
        *,
        opening_context: OpeningContextProjection,
        repair: bool = False,
        repair_issues: Sequence[OpeningValidationIssue] = (),
        repair_plan_skeleton: dict[str, object] | None = None,
    ) -> RunOpeningDecision | AgentFailure: ...

    async def finalize_analysis_plan(
        self,
        request: AnalysisPlanFinalizationRequest,
        cancellation: CancellationSignal,
    ) -> AnalysisPlanningDraft | AgentFailure: ...

    async def invoke_with_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        cancellation: CancellationSignal,
    ) -> AIMessage | AgentFailure: ...

    async def generate_final_answer(
        self,
        messages: Sequence[BaseMessage],
        mode: FinalOutputMode,
        cancellation: CancellationSignal,
    ) -> FinalMarkdownPayload | AgentFailure: ...

    async def generate_final_answer_stream(
        self,
        messages: Sequence[BaseMessage],
        cancellation: CancellationSignal,
    ) -> AsyncIterator[str | AgentFailure] | AgentFailure: ...


class DataGatewayPort(Protocol):
    """读取固定 Schema 和执行只读 SQL 的唯一 Agent 数据访问边界。"""

    async def load_schema(
        self,
        request: SchemaLoadRequest,
        cancellation: CancellationSignal,
    ) -> SchemaContext | AgentFailure: ...

    async def execute_readonly(
        self,
        request: SqlExecutionRequest,
        cancellation: CancellationSignal,
    ) -> SqlExecutionResult | SqlExecutionFailure | AgentFailure: ...


class DataLinkPort(Protocol):
    """查询 DataLink MCP 的业务边界，缓存和连接细节由适配层持有。"""

    async def explore(
        self,
        request: DataLinkExploreCommand,
        cancellation: CancellationSignal,
    ) -> DataLinkExploreResponse | AgentFailure: ...


class SandboxPort(Protocol):
    """在外层管理的受控工作区中执行一次命令，不暴露 Docker 细节。"""

    async def execute(
        self,
        request: SandboxExecutionRequest,
        cancellation: CancellationSignal,
    ) -> SandboxExecutionResult: ...


class ArtifactWriterPort(Protocol):
    """将已通过文件检查的相对路径登记为正式 Artifact。"""

    async def register_file(
        self,
        request: ArtifactRegistration,
        cancellation: CancellationSignal,
    ) -> ArtifactRef | AgentFailure: ...


class ScriptWorkspacePort(Protocol):
    """Graph 只通过工作区 ID 写脚本，不接触宿主机路径。"""

    async def write_analysis_script(self, workspace_id: str, script: str) -> None: ...


class RunEventPublisherPort(Protocol):
    """将白名单事件交给运行时管道，Agent 不接触数据库或 SSE。"""

    async def publish(self, event: RunEventCreate) -> AgentFailure | None: ...

    def transaction(self, run_id: str) -> AbstractAsyncContextManager[None]: ...

    async def append(self, event: RunEventCreate) -> object: ...

    async def notify_committed(self, run_id: str) -> None: ...
