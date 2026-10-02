from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from agent_runtime.contracts import AgentArtifactType, AgentErrorCode, ArtifactRegistration
from application.result_outputs import ResultOutputService
from application.script_workspace import ScriptWorkspaceManager
from pydantic import ValidationError


class FakeCancellation:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeRepository:
    def __init__(self) -> None:
        self.kwargs = None
        self.deleted = False

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(id=kwargs["artifact_id"])

    async def delete(self, _record) -> None:
        self.deleted = True


async def _service(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    workspaces = ScriptWorkspaceManager(
        datasource_root=tmp_path / "sources",
        workspace_root=tmp_path / "workspaces",
        runtime_trace_root=tmp_path / "traces",
    )
    workspace = await workspaces.create(run_id="run_outputs", input_snapshot_path=str(source))
    repository = FakeRepository()
    service = ResultOutputService(
        workspaces=workspaces,
        repository=repository,
        artifact_root=tmp_path / "artifacts",
        run_id="run_outputs",
        session_id="session_outputs",
    )
    return service, workspaces, workspace, repository


@pytest.mark.asyncio
async def test_register_file_copies_declared_output_and_keeps_values(tmp_path) -> None:
    service, workspaces, workspace, repository = await _service(tmp_path)
    await workspaces.prepare_outputs(workspace.workspace_id, ["outputs/summary.csv"])
    (workspace.outputs_dir / "summary.csv").write_text(
        "email\nuser@example.com\n", encoding="utf-8"
    )
    request = ArtifactRegistration(
        workspace_id=workspace.workspace_id,
        relative_path="outputs/summary.csv",
        type=AgentArtifactType.FILE,
        title="原始摘要",
        purpose="内部分析结果",
        source_tool_call_id="tool_outputs",
    )

    artifact = await service.register_file(request, FakeCancellation())

    assert artifact.artifact_id.startswith("artifact_")
    assert repository.kwargs is not None
    assert repository.kwargs["tool_call_id"] == "tool_outputs"
    assert repository.kwargs["metadata_json"]["source"] == "python_output"
    assert repository.kwargs["metadata_json"] == {
        "source": "python_output",
        "purpose": "内部分析结果",
    }
    stored = next((tmp_path / "artifacts" / "runs" / "run_outputs").iterdir())
    assert stored.read_text(encoding="utf-8") == "email\nuser@example.com\n"
    assert repository.kwargs["content_hash"] == hashlib.sha256(stored.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_register_file_rejects_missing_or_unsafe_output(tmp_path) -> None:
    service, _workspaces, workspace, _repository = await _service(tmp_path)
    request = ArtifactRegistration(
        workspace_id=workspace.workspace_id,
        relative_path="outputs/missing.csv",
        type=AgentArtifactType.FILE,
        title="缺失",
        purpose="测试",
    )

    result = await service.register_file(request, FakeCancellation())

    assert result.code is AgentErrorCode.ARTIFACT_REJECTED

    with pytest.raises(ValidationError):
        ArtifactRegistration(
            workspace_id=workspace.workspace_id,
            relative_path="safe_results/data.csv",
            type=AgentArtifactType.FILE,
            title="旧路径",
            purpose="测试",
        )


@pytest.mark.asyncio
async def test_register_file_rejects_unsafe_svg_but_allows_other_outputs(tmp_path) -> None:
    service, workspaces, workspace, _repository = await _service(tmp_path)
    await workspaces.prepare_outputs(workspace.workspace_id, ["charts/chart.svg"])
    (workspace.charts_dir / "chart.svg").write_text(
        '<svg onload="alert(1)"></svg>', encoding="utf-8"
    )
    request = ArtifactRegistration(
        workspace_id=workspace.workspace_id,
        relative_path="charts/chart.svg",
        type=AgentArtifactType.CHART,
        title="图表",
        purpose="测试",
    )

    result = await service.register_file(request, FakeCancellation())

    assert result.code is AgentErrorCode.ARTIFACT_REJECTED

    (workspace.charts_dir / "chart.svg").write_text(
        """<?xml version="1.0" encoding="utf-8" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"
  "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <rect width="10" height="10" fill="#35705c"/>
</svg>
""",
        encoding="utf-8",
    )

    accepted = await service.register_file(request, FakeCancellation())

    assert accepted.type is AgentArtifactType.CHART
