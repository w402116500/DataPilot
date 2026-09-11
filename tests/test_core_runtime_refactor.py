from __future__ import annotations

import pytest
from agent_runtime.analysis_planning import MaterializedAnalysisPlan
from agent_runtime.contracts import (
    AgentArtifactType,
    AnalysisExecutionConstraints,
    ArtifactRef,
    FinalMarkdownPayload,
    RunContext,
    SandboxExecutionResult,
    SandboxExecutionStatus,
    SchemaContext,
)
from agent_runtime.graph import GraphDependencies, _execute_python, _NeverCanceled
from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead
from langchain_core.messages import AIMessage


class FakeModel:
    def __init__(self) -> None:
        self.tools: list[list[str]] = []
        self.agent_responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_python",
                        "name": "run_python",
                        "args": {
                            "script": "print('chart')",
                            "output_paths": ["charts/result.png"],
                            "purpose": "生成趋势图",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="图表已生成"),
        ]

    async def invoke_with_tools(self, _messages, tools, _cancellation):
        self.tools.append([item.name for item in tools])
        return self.agent_responses.pop(0)

    async def generate_final_answer(self, _messages, _mode, _cancellation):
        return FinalMarkdownPayload(markdown="图表已生成。")

    async def generate_final_answer_stream(self, _messages, _cancellation):
        async def chunks():
            yield "图表已生成。"

        return chunks()


class FakeGateway:
    async def load_schema(self, request, _cancellation):
        return SchemaContext(
            datasource_id=request.datasource_id,
            schema_revision=request.schema_revision,
            schema_summary=SchemaSummaryRead(
                datasource_id=request.datasource_id,
                dialect="sqlite",
                tables=[
                    SchemaTableRead(
                        name="sales",
                        columns=[SchemaColumnRead(name="amount", type="INTEGER", nullable=False)],
                    )
                ],
            ),
        )

    async def execute_readonly(self, _request, _cancellation):
        raise AssertionError("Python 测试不应调用 SQL")


class FakeWorkspaces:
    async def write_analysis_script(self, workspace_id: str, script: str) -> None:
        assert workspace_id == "workspace_1"
        assert script.startswith("print")


class FakeSandbox:
    async def execute(self, request, _cancellation):
        assert request.output_paths == ["charts/result.png"]
        assert request.purpose == "生成趋势图"
        return SandboxExecutionResult(
            status=SandboxExecutionStatus.COMPLETED,
            elapsed_ms=4,
            exit_code=0,
            stdout="done",
        )


class FakeArtifacts:
    async def register_file(self, request, _cancellation):
        return ArtifactRef(
            artifact_id="artifact_chart",
            type=AgentArtifactType.CHART,
            title="result.png",
            source_tool_call_id=request.source_tool_call_id,
        )


def _context() -> RunContext:
    return RunContext(
        run_id="run_1",
        session_id="session_1",
        datasource_id="datasource_1",
        model_profile_id="profile_1",
        model_name="demo",
        schema_revision=1,
        input_snapshot_ref="snapshots/run_1/source.csv",
        input_filename="source.csv",
        question="生成趋势图",
    )


def _plan() -> MaterializedAnalysisPlan:
    return MaterializedAnalysisPlan(
        mode="ready",
        requirements=(),
        constraints=AnalysisExecutionConstraints(
            required_artifacts=[{"kind": "chart", "minimum_count": 1, "description": "生成趋势图"}]
        ),
    )


@pytest.mark.asyncio
async def test_python_tool_uses_run_local_context_and_registers_only_declared_outputs() -> None:
    model = FakeModel()

    observation, artifacts, warning = await _execute_python(
        _context(),
        GraphDependencies(
            model=model,
            gateway=FakeGateway(),
            plan=_plan(),
            workspace_id="workspace_1",
            workspaces=FakeWorkspaces(),
            sandbox=FakeSandbox(),
            artifacts=FakeArtifacts(),
        ),
        _NeverCanceled(),
        "call_python",
        {
            "script": "print('chart')",
            "output_paths": ["charts/result.png"],
            "purpose": "生成趋势图",
        },
    )

    assert observation.status == "succeeded"
    assert [artifact.artifact_id for artifact in artifacts] == ["artifact_chart"]
    assert warning is None
