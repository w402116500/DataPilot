from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime.contracts import AgentErrorCode, SandboxExecutionRequest, SandboxExecutionStatus
from application.docker_sandbox import DockerCommandResult, DockerSandbox
from application.script_workspace import ScriptWorkspaceManager


class FakeCancellation:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeRunner:
    def __init__(self, result: DockerCommandResult, expected_timeout: int = 30) -> None:
        self.result = result
        self.expected_timeout = expected_timeout
        self.arguments: list[str] | None = None
        self.removed: list[str] = []

    async def run(self, arguments, *, timeout_seconds, cancellation):
        self.arguments = list(arguments)
        assert timeout_seconds == self.expected_timeout
        return self.result

    async def remove_container(self, container_name: str) -> None:
        self.removed.append(container_name)


async def _workspace(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    manager = ScriptWorkspaceManager(
        datasource_root=tmp_path / "sources",
        workspace_root=tmp_path / "workspaces",
        runtime_trace_root=tmp_path / "traces",
    )
    return manager, await manager.create(run_id="run_1", input_snapshot_path=str(source))


def test_sandbox_image_provides_a_chinese_matplotlib_font() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1] / "apps" / "api" / "sandbox" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "fontconfig" in dockerfile
    assert "fonts-noto-cjk" in dockerfile
    assert "Noto Sans CJK JP" in dockerfile
    assert "fc-cache --force" in dockerfile
    assert "> /tmp/matplotlib/matplotlibrc" in dockerfile


@pytest.mark.asyncio
async def test_sandbox_mounts_only_input_script_and_declared_output_files(tmp_path) -> None:
    manager, workspace = await _workspace(tmp_path)
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")
    runner = FakeRunner(DockerCommandResult(exit_code=0, stdout="ok", stderr=""))
    sandbox = DockerSandbox(workspaces=manager, image="demo:latest", runner=runner)

    result = await sandbox.execute(
        SandboxExecutionRequest(
            workspace_id=workspace.workspace_id,
            command=("python", "analysis.py"),
            output_paths=["outputs/result.csv", "charts/chart.png"],
            purpose="analysis",
        ),
        FakeCancellation(),
    )

    assert result.status is SandboxExecutionStatus.COMPLETED
    assert runner.arguments is not None
    assert "raw_results" not in " ".join(runner.arguments)
    assert "safe_results" not in " ".join(runner.arguments)
    assert any("dst=/workspace/input" in item and "readonly" in item for item in runner.arguments)
    assert any("dst=/workspace/work/outputs/result.csv" in item for item in runner.arguments)
    assert any("dst=/workspace/work/charts/chart.png" in item for item in runner.arguments)
    assert runner.removed == [f"datapilot-sandbox-{workspace.workspace_id}"]


@pytest.mark.asyncio
async def test_sandbox_caps_configured_timeout_by_run_remaining_time(tmp_path) -> None:
    manager, workspace = await _workspace(tmp_path)
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")
    runner = FakeRunner(
        DockerCommandResult(exit_code=0, stdout="ok", stderr=""),
        expected_timeout=12,
    )
    sandbox = DockerSandbox(
        workspaces=manager,
        image="demo:latest",
        timeout_seconds=30,
        timeout_provider=lambda: 12.8,
        runner=runner,
    )

    result = await sandbox.execute(
        SandboxExecutionRequest(
            workspace_id=workspace.workspace_id,
            command=("python", "analysis.py"),
            output_paths=["charts/chart.png"],
            purpose="analysis",
        ),
        FakeCancellation(),
    )

    assert result.status is SandboxExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_sandbox_truncates_success_stderr_to_contract_limit(tmp_path) -> None:
    manager, workspace = await _workspace(tmp_path)
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")
    runner = FakeRunner(DockerCommandResult(exit_code=0, stdout="ok", stderr="w" * 8_001))
    sandbox = DockerSandbox(workspaces=manager, image="demo:latest", runner=runner)

    result = await sandbox.execute(
        SandboxExecutionRequest(
            workspace_id=workspace.workspace_id,
            command=("python", "analysis.py"),
            output_paths=["charts/chart.png"],
            purpose="analysis",
        ),
        FakeCancellation(),
    )

    assert result.status is SandboxExecutionStatus.COMPLETED
    assert result.stderr == "w" * 8_000


@pytest.mark.asyncio
async def test_sandbox_rejects_undeclared_or_unsupported_output_path(tmp_path) -> None:
    manager, workspace = await _workspace(tmp_path)
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")
    runner = FakeRunner(DockerCommandResult(exit_code=0, stdout="", stderr=""))
    sandbox = DockerSandbox(workspaces=manager, image="demo:latest", runner=runner)

    result = await sandbox.execute(
        SandboxExecutionRequest(
            workspace_id=workspace.workspace_id,
            command=("python", "analysis.py"),
            output_paths=["outputs/unknown.html"],
            purpose="analysis",
        ),
        FakeCancellation(),
    )

    assert result.status is SandboxExecutionStatus.REJECTED
    assert runner.arguments is None


@pytest.mark.asyncio
async def test_sandbox_classifies_network_failure_without_returning_stderr(tmp_path) -> None:
    manager, workspace = await _workspace(tmp_path)
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")
    runner = FakeRunner(
        DockerCommandResult(
            exit_code=1,
            stdout="secret output",
            stderr="Temporary failure in name resolution",
        )
    )
    sandbox = DockerSandbox(workspaces=manager, image="demo:latest", runner=runner)

    result = await sandbox.execute(
        SandboxExecutionRequest(
            workspace_id=workspace.workspace_id,
            command=("python", "analysis.py"),
            output_paths=["outputs/result.csv"],
            purpose="analysis",
        ),
        FakeCancellation(),
    )

    assert result.status is SandboxExecutionStatus.NETWORK_DENIED
    assert result.failure is not None
    assert result.failure.message != "Temporary failure in name resolution"


@pytest.mark.asyncio
async def test_sandbox_classifies_unavailable_docker_desktop_daemon_without_raw_error(
    tmp_path,
) -> None:
    manager, workspace = await _workspace(tmp_path)
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")
    runner = FakeRunner(
        DockerCommandResult(
            exit_code=1,
            stdout="",
            stderr=(
                "failed to connect to the docker API at "
                "npipe:////./pipe/dockerDesktopLinuxEngine; "
                "check if the daemon is running"
            ),
        )
    )
    sandbox = DockerSandbox(workspaces=manager, image="demo:latest", runner=runner)

    result = await sandbox.execute(
        SandboxExecutionRequest(
            workspace_id=workspace.workspace_id,
            command=("python", "analysis.py"),
            output_paths=["report.md"],
            purpose="analysis",
        ),
        FakeCancellation(),
    )

    assert result.status is SandboxExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.code is AgentErrorCode.SANDBOX_FAILED
    assert result.failure.retryable is False
    assert result.failure.message == "Sandbox 运行环境不可用"
    assert result.stdout == ""
    assert result.stderr == ""
