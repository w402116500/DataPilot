"""登记 Python 明确声明的产物，不负责猜测或改写脚本数据。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import struct
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4
from xml.etree import ElementTree

from agent_runtime.contracts import (
    AgentArtifactType,
    AgentErrorCode,
    AgentFailure,
    ArtifactRef,
    ArtifactRegistration,
)
from agent_runtime.ports import CancellationSignal
from metadata.repositories import ArtifactRepository

from application.script_workspace import ScriptWorkspaceError, ScriptWorkspaceManager

_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
_ARTIFACT_SUFFIXES = frozenset({".csv", ".json", ".md", ".png", ".svg", ".tsv", ".txt", ".xlsx"})
_MIME_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_UNSAFE_SVG_ELEMENTS = frozenset({"script", "foreignobject"})
_UNSAFE_TEXT_MARKERS = (
    "<script",
    "<iframe",
    "<object",
    "<embed",
    "<link",
    "javascript:",
    "data:",
    "http://",
    "https://",
    "url(",
)
_HTML_EVENT_ATTRIBUTE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)


class ResultOutputService:
    """校验、复制并登记本次 Run 已声明的 Python 输出文件。"""

    def __init__(
        self,
        *,
        workspaces: ScriptWorkspaceManager,
        repository: ArtifactRepository,
        artifact_root: Path,
        run_id: str,
        session_id: str,
    ) -> None:
        self.workspaces = workspaces
        self.repository = repository
        self.artifact_root = artifact_root.resolve()
        self.run_id = run_id
        self.session_id = session_id

    async def register_file(
        self,
        request: ArtifactRegistration,
        cancellation: CancellationSignal,
    ) -> ArtifactRef | AgentFailure:
        """只登记模型声明且位于受控工作区的单个文件。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        target: Path | None = None
        try:
            workspace = self.workspaces.get(request.workspace_id)
            source = await asyncio.to_thread(self._resolve_source, workspace, request)
            artifact_id = f"artifact_{uuid4().hex}"
            suffix = source.suffix.lower()
            storage_ref = f"runs/{self.run_id}/{artifact_id}{suffix}"
            target = self._resolve_target(storage_ref)
            content_hash, size_bytes = await asyncio.to_thread(
                self._copy_file_atomically,
                source,
                target,
            )
            if cancellation.is_cancelled():
                await asyncio.to_thread(_remove_file, target)
                return _cancelled_failure()
            record = await self.repository.create(
                artifact_id=artifact_id,
                run_id=self.run_id,
                session_id=self.session_id,
                tool_call_id=request.source_tool_call_id,
                artifact_type=request.type.value,
                title=request.title,
                storage_ref=storage_ref,
                mime_type=_MIME_TYPES[suffix],
                size_bytes=size_bytes,
                preview_json=None,
                metadata_json={
                    "source": "python_output",
                    "purpose": request.purpose,
                },
                content_hash=content_hash,
            )
            if cancellation.is_cancelled():
                await self.repository.delete(record)
                await asyncio.to_thread(_remove_file, target)
                return _cancelled_failure()
        except (OSError, ScriptWorkspaceError, ValueError, UnicodeError):
            if target is not None:
                await asyncio.to_thread(_remove_file, target)
            return AgentFailure(
                code=AgentErrorCode.ARTIFACT_REJECTED,
                message="脚本产物不符合交付安全规则",
            )
        except Exception:
            if target is not None:
                await asyncio.to_thread(_remove_file, target)
            raise
        return ArtifactRef(
            artifact_id=record.id,
            type=request.type,
            title=request.title,
            source_tool_call_id=request.source_tool_call_id,
        )

    def _resolve_source(self, workspace, request: ArtifactRegistration) -> Path:
        relative = _safe_relative(request.relative_path)
        target = (workspace.work_dir / Path(*relative.parts)).resolve()
        allowed_roots = (workspace.outputs_dir, workspace.charts_dir)
        if target != workspace.report_path.resolve() and not any(
            _is_within(target, root.resolve()) for root in allowed_roots
        ):
            raise ScriptWorkspaceError("Artifact 路径不在当前 Run 输出目录")
        if _contains_symlink(workspace.work_dir, relative.parts) or not target.is_file():
            raise ScriptWorkspaceError("Artifact 文件不存在")
        suffix = target.suffix.lower()
        if suffix not in _ARTIFACT_SUFFIXES or target.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise ScriptWorkspaceError("Artifact 文件类型或大小无效")
        expected_type = _expected_artifact_type(relative.as_posix(), suffix)
        if request.type is not expected_type:
            raise ScriptWorkspaceError("Artifact 类型与输出路径不一致")
        _validate_content(target, suffix, request.type)
        return target

    def _resolve_target(self, storage_ref: str) -> Path:
        relative = PurePosixPath(storage_ref)
        windows = PureWindowsPath(storage_ref)
        if (
            not storage_ref
            or relative.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or ".." in relative.parts
            or "\\" in storage_ref
        ):
            raise ValueError("Artifact storage reference is invalid")
        target = (self.artifact_root / Path(*relative.parts)).resolve()
        if not _is_within(target, self.artifact_root):
            raise ValueError("Artifact storage reference escaped configured root")
        return target

    @staticmethod
    def _copy_file_atomically(source: Path, target: Path) -> tuple[str, int]:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                while chunk := source_handle.read(64 * 1024):
                    target_handle.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return digest.hexdigest(), size_bytes


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ScriptWorkspaceError("Artifact 相对路径无效")
    return path


def _expected_artifact_type(relative_path: str, suffix: str) -> AgentArtifactType:
    if relative_path.startswith("charts/") and suffix in {".png", ".svg"}:
        return AgentArtifactType.CHART
    if relative_path == "report.md" or suffix == ".md":
        return AgentArtifactType.MARKDOWN
    return AgentArtifactType.FILE


def _validate_content(path: Path, suffix: str, artifact_type: AgentArtifactType) -> None:
    if artifact_type is AgentArtifactType.CHART and suffix == ".png":
        if not _is_valid_png(path.read_bytes()):
            raise ValueError("PNG 内容无效")
    elif artifact_type is AgentArtifactType.CHART and suffix == ".svg":
        content = path.read_text(encoding="utf-8")
        if not _is_safe_svg(content):
            raise ValueError("SVG 内容不安全")
    elif artifact_type is AgentArtifactType.MARKDOWN:
        content = path.read_text(encoding="utf-8").lower()
        if any(
            marker in content for marker in _UNSAFE_TEXT_MARKERS
        ) or _HTML_EVENT_ATTRIBUTE.search(content):
            raise ValueError("Markdown 内容不安全")


def _is_valid_png(content: bytes) -> bool:
    if not content.startswith(_PNG_SIGNATURE):
        return False
    offset = len(_PNG_SIGNATURE)
    saw_header = False
    while offset + 12 <= len(content):
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(content):
            return False
        chunk_type = content[offset + 4 : offset + 8]
        chunk_data = content[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", content[offset + 8 + length : end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            return False
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                return False
            saw_header = True
        if chunk_type == b"IEND":
            return saw_header and length == 0 and end == len(content)
        offset = end
    return False


def _has_external_svg_reference(content: str) -> bool:
    for match in re.finditer(r"(?:xlink:)?href\s*=\s*(['\"])(.*?)\1", content):
        if not match.group(2).startswith("#"):
            return True
    for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", content):
        if not match.group(2).startswith("#"):
            return True
    return "@import" in content


def _is_safe_svg(content: str) -> bool:
    lowered = content.lower()
    if re.search(r"<!\s*entity\b", lowered):
        return False
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return False
    if root.tag not in {"svg", f"{{{_SVG_NAMESPACE}}}svg"}:
        return False
    if _HTML_EVENT_ATTRIBUTE.search(content) or _has_external_svg_reference(lowered):
        return False
    for element in root.iter():
        if _xml_local_name(element.tag).lower() in _UNSAFE_SVG_ELEMENTS:
            return False
        for attribute, value in element.attrib.items():
            name = _xml_local_name(attribute).lower()
            normalized = value.strip().lower()
            if name.startswith("on") or "javascript:" in normalized:
                return False
            if name == "href" and not normalized.startswith("#"):
                return False
    return True


def _xml_local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _contains_symlink(root: Path, parts: tuple[str, ...]) -> bool:
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _remove_file(path: Path) -> None:
    if path.exists() and path.is_file():
        path.unlink()


def _cancelled_failure() -> AgentFailure:
    return AgentFailure(code=AgentErrorCode.RUN_CANCELED, message="分析已取消")
