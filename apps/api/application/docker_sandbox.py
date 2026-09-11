"""以固定 Docker 参数执行 ``analysis.py``，不向 Agent 暴露容器细节。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExecutionStatus,
)
from agent_runtime.ports import CancellationSignal

from application.script_workspace import (
    ScriptWorkspace,
    ScriptWorkspaceError,
    ScriptWorkspaceManager,
)

logger = logging.getLogger(__name__)

_NETWORK_ERROR_MARKERS = (
    "network is unreachable",
    "network unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to establish a new connection",
)
_SECURITY_ERROR_MARKERS = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "mounts denied",
)
_RUNTIME_UNAVAILABLE_MARKERS = (
    "failed to connect to the docker api",
    "is the docker daemon running",
    "cannot connect to the docker daemon",
    "no such image",
    "pull access denied",
)


@dataclass(frozen=True)
class DockerCommandResult:
    """Docker CLI 的内部原始结果，不能越过应用层进入 Graph。"""

    exit_code: int
    stderr: str
    stdout: str = ""


class DockerCommandTimedOut(TimeoutError):
    """Docker CLI 在限定时间内没有结束。"""


class DockerCommandCanceled(RuntimeError):
    """运行过程中收到取消信号。"""


class DockerRunner(Protocol):
    """可替换 Docker CLI，单元测试不需要真实 Docker 守护进程。"""

    async def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: int,
        cancellation: CancellationSignal,
    ) -> DockerCommandResult: ...

    async def remove_container(self, container_name: str) -> None: ...


class SubprocessDockerRunner:
    """使用 Docker CLI 执行一次性容器，并在超时或取消时终止 CLI。"""

    def __init__(self, executable: str = "docker") -> None:
        self.executable = executable

    async def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: int,
        cancellation: CancellationSignal,
    ) -> DockerCommandResult:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communicate = asyncio.create_task(process.communicate())
        deadline = time.monotonic() + timeout_seconds
        try:
            while not communicate.done():
                if cancellation.is_cancelled():
                    await self._stop(process)
                    raise DockerCommandCanceled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._stop(process)
                    raise DockerCommandTimedOut
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(communicate), timeout=min(remaining, 0.1))
            stdout, stderr = await communicate
            return DockerCommandResult(
                exit_code=process.returncode or 0,
                stderr=stderr.decode("utf-8", errors="replace")[:16_384],
                stdout=stdout.decode("utf-8", errors="replace")[:8_000],
            )
        finally:
            if not communicate.done():
                await self._stop(process)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await communicate

    async def remove_container(self, container_name: str) -> None:
        """尽力删除命名容器；``docker run --rm`` 已覆盖正常退出。"""

        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "container",
                "rm",
                "--force",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            await self._stop(process)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()


class DockerSandbox:
    """SandboxPort 的应用层实现，固定资源、挂载和唯一 Python 入口。"""

    def __init__(
        self,
        *,
        workspaces: ScriptWorkspaceManager,
        image: str,
        timeout_seconds: int = 30,
        timeout_provider: Callable[[], float] | None = None,
        runner: DockerRunner | None = None,
    ) -> None:
        if not image or any(character.isspace() for character in image):
            raise ValueError("Sandbox image must be a non-empty Docker image reference")
        self.workspaces = workspaces
        self.image = image
        if timeout_seconds <= 0:
            raise ValueError("Sandbox timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self.timeout_provider = timeout_provider
        self.runner = runner or SubprocessDockerRunner()

    async def execute(
        self,
        request: SandboxExecutionRequest,
        cancellation: CancellationSignal,
    ) -> SandboxExecutionResult:
        """执行固定 ``python analysis.py``，仅返回稳定状态和安全摘要。"""

        started_at = time.perf_counter()
        if cancellation.is_cancelled():
            return self._canceled_result(started_at)
        if request.command != ("python", "analysis.py"):
            return self._rejected_result(started_at, "Sandbox 只允许执行固定分析脚本")
        try:
            workspace = self.workspaces.get(request.workspace_id)
        except ScriptWorkspaceError:
            return self._rejected_result(started_at, "Sandbox 工作区不可用")
        if not workspace.script_path.is_file() or workspace.script_path.is_symlink():
            return self._rejected_result(started_at, "分析脚本不可用")

        try:
            await self.workspaces.prepare_outputs(workspace.workspace_id, request.output_paths)
        except ScriptWorkspaceError:
            return self._rejected_result(started_at, "Sandbox 输出目录不可用")

        container_name = f"datapilot-sandbox-{workspace.workspace_id}"
        effective_timeout_seconds = float(self.timeout_seconds)
        if self.timeout_provider is not None:
            effective_timeout_seconds = min(effective_timeout_seconds, self.timeout_provider())
        if effective_timeout_seconds < 1:
            return self._failure_result(
                SandboxExecutionStatus.TIMED_OUT,
                AgentErrorCode.SANDBOX_TIMEOUT,
                "分析脚本没有剩余 Run 时间",
                started_at,
            )
        effective_timeout = max(1, int(effective_timeout_seconds))
        try:
            result = await self.runner.run(
                self._docker_arguments(workspace, container_name, request.output_paths),
                timeout_seconds=effective_timeout,
                cancellation=cancellation,
            )
        except DockerCommandCanceled:
            return self._canceled_result(started_at)
        except DockerCommandTimedOut:
            return self._failure_result(
                SandboxExecutionStatus.TIMED_OUT,
                AgentErrorCode.SANDBOX_TIMEOUT,
                f"分析脚本运行超过 {effective_timeout} 秒限制",
                started_at,
            )
        except OSError:
            return self._failure_result(
                SandboxExecutionStatus.FAILED,
                AgentErrorCode.SANDBOX_FAILED,
                "Sandbox 运行环境不可用",
                started_at,
                retryable=False,
            )
        finally:
            try:
                await self.runner.remove_container(container_name)
            except Exception:
                logger.warning(
                    "Sandbox container cleanup failed",
                    extra={"workspace_id": request.workspace_id},
                )

        if cancellation.is_cancelled():
            return self._canceled_result(started_at)
        if result.exit_code == 0:
            # 产物登记由下一层按同一份声明清单完成，Sandbox 只返回执行摘要。
            return SandboxExecutionResult(
                status=SandboxExecutionStatus.COMPLETED,
                elapsed_ms=self._elapsed_ms(started_at),
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr[-8_000:],
            )
        failure = self._classify_failed_command(result, started_at)
        if (
            failure.failure is not None
            and failure.failure.code is AgentErrorCode.SANDBOX_FAILED
            and not failure.failure.retryable
        ):
            logger.warning(
                "Sandbox runtime unavailable",
                extra={
                    "workspace_id": request.workspace_id,
                    "error_code": failure.failure.code.value,
                    "elapsed_ms": failure.elapsed_ms,
                },
            )
        return failure

    def _docker_arguments(
        self,
        workspace: ScriptWorkspace,
        container_name: str,
        output_paths: Sequence[str],
    ) -> list[str]:
        """构造唯一允许的 Docker 调用，不给模型提供可影响的参数槽位。"""

        arguments = [
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
            "--mount",
            f"type=bind,src={workspace.script_path},dst=/workspace/work/analysis.py,readonly",
        ]
        arguments.extend(
            [
                "--mount",
                f"type=bind,src={workspace.input_dir},dst=/workspace/input,readonly",
            ]
        )
        for output_path in output_paths:
            relative = PurePosixPath(output_path)
            source = workspace.work_dir.joinpath(*relative.parts)
            mount = f"type=bind,src={source},dst=/workspace/work/{relative.as_posix()}"
            arguments.extend(["--mount", mount])
        return [*arguments, self.image, "python", "analysis.py"]

    def _classify_failed_command(
        self,
        result: DockerCommandResult,
        started_at: float,
    ) -> SandboxExecutionResult:
        stderr = result.stderr.lower()
        if any(marker in stderr for marker in _NETWORK_ERROR_MARKERS):
            return self._failure_result(
                SandboxExecutionStatus.NETWORK_DENIED,
                AgentErrorCode.SANDBOX_NETWORK_DENIED,
                "分析脚本尝试访问网络，已被 Sandbox 阻止",
                started_at,
            )
        if any(marker in stderr for marker in _SECURITY_ERROR_MARKERS):
            return self._rejected_result(started_at, "分析脚本触发了 Sandbox 安全限制")
        if any(marker in stderr for marker in _RUNTIME_UNAVAILABLE_MARKERS):
            return self._failure_result(
                SandboxExecutionStatus.FAILED,
                AgentErrorCode.SANDBOX_FAILED,
                "Sandbox 运行环境不可用",
                started_at,
            )
        retryable = "error response from daemon" not in stderr
        return self._failure_result(
            SandboxExecutionStatus.FAILED,
            AgentErrorCode.SANDBOX_FAILED,
            "分析脚本执行失败",
            started_at,
            retryable=retryable,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _rejected_result(self, started_at: float, message: str) -> SandboxExecutionResult:
        return self._failure_result(
            SandboxExecutionStatus.REJECTED,
            AgentErrorCode.SANDBOX_REJECTED,
            message,
            started_at,
        )

    def _canceled_result(self, started_at: float) -> SandboxExecutionResult:
        return self._failure_result(
            SandboxExecutionStatus.CANCELED,
            AgentErrorCode.RUN_CANCELED,
            "分析已取消",
            started_at,
        )

    def _failure_result(
        self,
        status: SandboxExecutionStatus,
        code: AgentErrorCode,
        message: str,
        started_at: float,
        *,
        retryable: bool = False,
        stdout: str = "",
        stderr: str = "",
    ) -> SandboxExecutionResult:
        return SandboxExecutionResult(
            status=status,
            elapsed_ms=self._elapsed_ms(started_at),
            failure=AgentFailure(code=code, message=message, retryable=retryable),
            stdout=stdout[-8_000:],
            stderr=stderr[-8_000:],
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((time.perf_counter() - started_at) * 1000))
