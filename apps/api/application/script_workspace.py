"""管理单次 Run 的受控脚本工作区和内部脚本留痕。"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from agent_runtime.contracts import validate_python_output_path
from contracts.ids import make_id


class ScriptWorkspaceError(ValueError):
    """工作区参数或受限文件操作不符合安全边界时抛出的安全错误。"""


@dataclass(frozen=True)
class ScriptWorkspace:
    """应用层持有的工作区路径；该对象绝不能传入 Agent Runtime。"""

    workspace_id: str
    run_id: str
    root: Path
    input_dir: Path
    work_dir: Path
    input_file: Path | None
    script_path: Path
    outputs_dir: Path
    charts_dir: Path
    report_path: Path


class ScriptWorkspaceManager:
    """创建、定位和清理 Run 专属工作区，并保留最后一版分析脚本。"""

    def __init__(
        self,
        *,
        datasource_root: Path,
        workspace_root: Path,
        runtime_trace_root: Path,
    ) -> None:
        self.datasource_root = datasource_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.runtime_trace_root = runtime_trace_root.resolve()
        self._active: dict[str, ScriptWorkspace] = {}

    async def create(
        self,
        *,
        run_id: str,
        source_ref: str = "",
        input_snapshot_path: str | None = None,
    ) -> ScriptWorkspace:
        """创建本次 Run 的工作区；有快照时复制只读输入文件，无快照时保持空的 input/。"""

        workspace = await asyncio.to_thread(self._create, run_id, source_ref, input_snapshot_path)
        self._active[workspace.workspace_id] = workspace
        return workspace

    @asynccontextmanager
    async def open(self, *, run_id: str, source_ref: str) -> AsyncIterator[ScriptWorkspace]:
        """用 finally 保证所有正常和异常退出路径都会回收临时目录。"""

        source_path = await asyncio.to_thread(self._resolve_source_ref, source_ref)
        workspace = await self.create(run_id=run_id, input_snapshot_path=str(source_path))
        try:
            yield workspace
        finally:
            await self.cleanup(workspace.workspace_id)

    async def write_analysis_script(self, workspace_id: str, script: str) -> None:
        """只允许将模型脚本写到固定的 ``work/analysis.py`` 位置。"""

        workspace = self.get(workspace_id)
        await asyncio.to_thread(self._write_analysis_script, workspace, script)

    async def prepare_outputs(self, workspace_id: str, output_paths: list[str]) -> None:
        """清理并准备本次模型声明的输出文件，避免复用上次尝试的旧文件。"""

        workspace = self.get(workspace_id)
        await asyncio.to_thread(self._prepare_outputs, workspace, output_paths)

    async def cleanup(self, workspace_id: str) -> None:
        """留存脚本后删除整个临时工作区。"""

        workspace = self._active.get(workspace_id)
        if workspace is None:
            return
        try:
            await asyncio.to_thread(self._cleanup, workspace)
        finally:
            if not workspace.root.exists():
                self._active.pop(workspace_id, None)

    def get(self, workspace_id: str) -> ScriptWorkspace:
        """按系统生成的 ID 查找活动工作区，不接受外部路径。"""

        workspace = self._active.get(workspace_id)
        if workspace is None:
            raise ScriptWorkspaceError("Sandbox 工作区不存在或已清理")
        return workspace

    def _create(
        self, run_id: str, source_ref: str, input_snapshot_path: str | None = None
    ) -> ScriptWorkspace:
        self._validate_identifier(run_id, field="run_id")
        source_path: Path | None = None
        if input_snapshot_path is not None:
            source_path = Path(input_snapshot_path).resolve()
            if (
                not source_path.is_file()
                or source_path.is_symlink()
                or source_path.suffix.lower() not in {".csv", ".sqlite"}
            ):
                raise ScriptWorkspaceError("输入快照文件不可用")
        workspace_id = make_id("workspace")
        root = self._workspace_path(workspace_id)
        input_dir = root / "input"
        work_dir = root / "work"
        outputs_dir = work_dir / "outputs"
        charts_dir = work_dir / "charts"
        report_path = work_dir / "report.md"
        input_file = input_dir / source_path.name if source_path is not None else None
        script_path = work_dir / "analysis.py"

        try:
            input_dir.mkdir(parents=True, exist_ok=False)
            work_dir.mkdir(parents=True, exist_ok=True)
            outputs_dir.mkdir(parents=True)
            charts_dir.mkdir(parents=True)
            self._make_writable_for_sandbox(outputs_dir)
            self._make_writable_for_sandbox(charts_dir)
            if source_path is not None and input_file is not None:
                shutil.copyfile(source_path, input_file)
                self._make_readonly(input_file)
        except Exception:
            if root.exists():
                self._remove_workspace_root(root)
            raise

        return ScriptWorkspace(
            workspace_id=workspace_id,
            run_id=run_id,
            root=root,
            input_dir=input_dir,
            work_dir=work_dir,
            input_file=input_file,
            script_path=script_path,
            outputs_dir=outputs_dir,
            charts_dir=charts_dir,
            report_path=report_path,
        )

    def _resolve_source_ref(self, source_ref: str) -> Path:
        """沿用数据源存储的相对引用规则，拒绝越界和符号链接。"""

        relative = PurePosixPath(source_ref)
        windows_path = PureWindowsPath(source_ref)
        if (
            not source_ref
            or relative.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in relative.parts
            or "\\" in source_ref
        ):
            raise ScriptWorkspaceError("数据源文件引用无效")
        source_candidate = self.datasource_root / Path(*relative.parts)
        if self._contains_symlink(self.datasource_root, relative.parts):
            raise ScriptWorkspaceError("数据源文件不存在")
        source_path = source_candidate.resolve()
        try:
            source_path.relative_to(self.datasource_root)
        except ValueError as exc:
            raise ScriptWorkspaceError("数据源文件引用无效") from exc
        if source_path.suffix not in {".csv", ".sqlite"}:
            raise ScriptWorkspaceError("数据源文件类型不受支持")
        if not source_path.is_file() or source_path.is_symlink():
            raise ScriptWorkspaceError("数据源文件不存在")
        return source_path

    def _workspace_path(self, workspace_id: str) -> Path:
        self._validate_identifier(workspace_id, field="workspace_id")
        root = self.workspace_root / workspace_id
        resolved = root.resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ScriptWorkspaceError("工作区路径无效") from exc
        return resolved

    def _write_analysis_script(self, workspace: ScriptWorkspace, script: str) -> None:
        encoded = script.encode("utf-8")
        if not encoded or len(encoded) > 256 * 1024:
            raise ScriptWorkspaceError("分析脚本大小无效")
        temporary = workspace.work_dir / f".analysis-{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
            os.replace(temporary, workspace.script_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _prepare_outputs(self, workspace: ScriptWorkspace, output_paths: list[str]) -> None:
        if not output_paths:
            raise ScriptWorkspaceError("至少声明一个 Python 输出文件")
        for output_path in output_paths:
            relative = self._validate_output_path(output_path)
            target = self._output_target(workspace, relative)
            if self._contains_symlink(workspace.work_dir, relative.parts):
                raise ScriptWorkspaceError("脚本输出路径包含不允许的链接")
            if target.exists():
                if target.is_dir():
                    raise ScriptWorkspaceError("脚本输出路径不能是目录")
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=False)
            self._make_writable_for_sandbox(target)

    @staticmethod
    def _validate_output_path(value: str) -> PurePosixPath:
        try:
            return PurePosixPath(validate_python_output_path(value))
        except ValueError as exc:
            raise ScriptWorkspaceError("脚本输出路径或格式不受支持") from exc

    @staticmethod
    def _output_target(workspace: ScriptWorkspace, relative: PurePosixPath) -> Path:
        target = (workspace.work_dir / Path(*relative.parts)).resolve()
        try:
            target.relative_to(workspace.work_dir.resolve())
        except ValueError as exc:
            raise ScriptWorkspaceError("脚本输出路径越界") from exc
        return target

    def _cleanup(self, workspace: ScriptWorkspace) -> None:
        try:
            self._persist_script_trace(workspace)
        finally:
            self._remove_workspace_root(workspace.root)

    def _persist_script_trace(self, workspace: ScriptWorkspace) -> None:
        """运行留痕只保留给内部排查，不登记为用户可见 Artifact。"""

        if not workspace.script_path.is_file() or workspace.script_path.is_symlink():
            return
        self._validate_identifier(workspace.run_id, field="run_id")
        trace_dir = (self.runtime_trace_root / workspace.run_id).resolve()
        try:
            trace_dir.relative_to(self.runtime_trace_root)
        except ValueError as exc:
            raise ScriptWorkspaceError("运行留痕路径无效") from exc
        trace_dir.mkdir(parents=True, exist_ok=True)
        target = trace_dir / "analysis.py"
        temporary = trace_dir / f".analysis-{uuid4().hex}.tmp"
        try:
            shutil.copyfile(workspace.script_path, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _remove_workspace_root(self, root: Path) -> None:
        resolved = root.resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ScriptWorkspaceError("拒绝清理工作区根目录外的路径") from exc
        if resolved.parent != self.workspace_root or not resolved.exists():
            raise ScriptWorkspaceError("拒绝清理非工作区目录")
        shutil.rmtree(resolved, onexc=self._make_removable_for_cleanup)

    @staticmethod
    def _contains_symlink(root: Path, parts: tuple[str, ...]) -> bool:
        """逐段拒绝符号链接，避免最终 ``resolve`` 后掩盖中间目录跳转。"""

        candidate = root
        for part in parts:
            candidate /= part
            if candidate.is_symlink():
                return True
        return False

    @staticmethod
    def _make_removable_for_cleanup(function, path: str, exception_info) -> None:
        """Windows 删除只读输入副本前恢复写位，且只由已验证的 rmtree 调用。"""

        del exception_info
        os.chmod(path, stat.S_IWRITE)
        function(path)

    @staticmethod
    def _make_readonly(path: Path) -> None:
        """在宿主机先去掉写位，Docker 还会以只读挂载再次限制输入目录。"""

        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    @staticmethod
    def _make_writable_for_sandbox(path: Path) -> None:
        """让固定镜像中的非 root 用户能写工作目录，输入目录不使用这个权限。"""

        path.chmod(0o777)

    @staticmethod
    def _validate_identifier(value: str, *, field: str) -> None:
        if (
            not value
            or len(value) > 120
            or not all(char.isascii() and (char.isalnum() or char in "_-") for char in value)
        ):
            raise ScriptWorkspaceError(f"{field} 无效")


@dataclass(frozen=True)
class SessionInputSnapshot:
    """Session 工作区中跨 Run 保留的一份只读数据输入快照。"""

    session_id: str
    run_id: str
    relative_ref: str
    filename: str
    path: Path


class SessionWorkspaceManager:
    """管理 Session 级输入快照；删除 Session 时由上层一次性清理目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def create_input_snapshot(
        self,
        *,
        session_id: str,
        run_id: str,
        source_path: Path,
    ) -> SessionInputSnapshot:
        return await asyncio.to_thread(
            self._create_input_snapshot,
            session_id,
            run_id,
            source_path,
        )

    async def resolve_input_snapshot(
        self,
        *,
        session_id: str,
        relative_ref: str,
    ) -> Path:
        return await asyncio.to_thread(self._resolve_input_snapshot, session_id, relative_ref)

    async def delete_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._delete_session, session_id)

    def _create_input_snapshot(
        self,
        session_id: str,
        run_id: str,
        source_path: Path,
    ) -> SessionInputSnapshot:
        self._validate_identifier(session_id, field="session_id")
        self._validate_identifier(run_id, field="run_id")
        source = source_path.resolve()
        if (
            not source.is_file()
            or source.is_symlink()
            or source.suffix.lower() not in {".csv", ".sqlite"}
        ):
            raise ScriptWorkspaceError("输入快照源文件不可用")
        target_dir = (self.root / session_id / "snapshots" / run_id).resolve()
        self._ensure_within(target_dir, self.root)
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / source.name
        try:
            shutil.copyfile(source, target)
            ScriptWorkspaceManager._make_readonly(target)
        except Exception:
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            raise
        relative_ref = PurePosixPath("snapshots", run_id, source.name).as_posix()
        return SessionInputSnapshot(session_id, run_id, relative_ref, source.name, target)

    def _resolve_input_snapshot(self, session_id: str, relative_ref: str) -> Path:
        self._validate_identifier(session_id, field="session_id")
        relative = PurePosixPath(relative_ref)
        windows = PureWindowsPath(relative_ref)
        if (
            not relative_ref
            or "\\" in relative_ref
            or relative.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in relative.parts)
            or len(relative.parts) != 3
            or relative.parts[0] != "snapshots"
        ):
            raise ScriptWorkspaceError("输入快照引用无效")
        target = (self.root / session_id / Path(*relative.parts)).resolve()
        self._ensure_within(target, (self.root / session_id).resolve())
        if (
            self._contains_symlink((self.root / session_id).resolve(), relative.parts)
            or not target.is_file()
        ):
            raise ScriptWorkspaceError("输入快照不存在")
        if target.suffix.lower() not in {".csv", ".sqlite"}:
            raise ScriptWorkspaceError("输入快照类型不受支持")
        return target

    def _delete_session(self, session_id: str) -> None:
        self._validate_identifier(session_id, field="session_id")
        target = (self.root / session_id).resolve()
        self._ensure_within(target, self.root)
        if target.exists():
            shutil.rmtree(target, onexc=ScriptWorkspaceManager._make_removable_for_cleanup)

    @staticmethod
    def _ensure_within(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ScriptWorkspaceError("工作区路径越界") from exc

    @staticmethod
    def _contains_symlink(root: Path, parts: tuple[str, ...]) -> bool:
        current = root
        for part in parts:
            current /= part
            if current.is_symlink():
                return True
        return False

    @staticmethod
    def _validate_identifier(value: str, *, field: str) -> None:
        if (
            not value
            or len(value) > 120
            or not all(char.isascii() and (char.isalnum() or char in "_-") for char in value)
        ):
            raise ScriptWorkspaceError(f"{field} 无效")
