from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from contracts.datasources import ArtifactUsage, TableDataRead
from contracts.ids import make_id
from metadata.repositories import ArtifactRepository


class ArtifactStore:
    """负责表格 Artifact 文件与 Metadata 的一致登记。"""

    def __init__(self, repository: ArtifactRepository, artifact_root: Path) -> None:
        self.repository = repository
        self.artifact_root = artifact_root.resolve()

    async def create_table_artifact(
        self,
        *,
        result: TableDataRead,
        title: str,
        run_id: str | None,
        session_id: str | None,
        tool_call_id: str | None,
        artifact_usage: ArtifactUsage = "query_result",
    ) -> str:
        """先尝试写完整文件，失败时以 preview_json 降级保存同一份安全结果。"""

        artifact_id = make_id("artifact")
        payload = result.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        content_hash = hashlib.sha256(encoded).hexdigest()
        storage_ref = f"gateway/{artifact_id}.json"
        target = self._resolve_storage_ref(storage_ref)
        metadata = {}
        if artifact_usage != "query_result":
            metadata["artifact_usage"] = artifact_usage
        try:
            await asyncio.to_thread(self._write_bytes, target, encoded)
        except OSError:
            record = await self.repository.create(
                artifact_id=artifact_id,
                run_id=run_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                title=title,
                storage_ref=None,
                size_bytes=0,
                preview_json=payload,
                metadata_json={"full_result": False, **metadata},
                content_hash=content_hash,
            )
            return record.id

        try:
            record = await self.repository.create(
                artifact_id=artifact_id,
                run_id=run_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                title=title,
                storage_ref=storage_ref,
                size_bytes=len(encoded),
                # 完整结果已由受控文件保存，Metadata 只保留索引，避免把大结果复制进数据库。
                preview_json=None,
                metadata_json={"full_result": True, **metadata},
                content_hash=content_hash,
            )
        except Exception:
            await asyncio.to_thread(target.unlink, True)
            raise
        return record.id

    def _resolve_storage_ref(self, storage_ref: str) -> Path:
        """将内部存储引用限定在 artifact_root 内，拒绝越界路径。"""

        relative = PurePosixPath(storage_ref)
        windows_path = PureWindowsPath(storage_ref)
        if (
            not storage_ref
            or relative.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in relative.parts
            or "\\" in storage_ref
        ):
            raise ValueError("Artifact storage_ref escaped the configured root")
        candidate = self.artifact_root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError("Artifact storage_ref cannot contain a symbolic link")
        target = candidate.resolve()
        try:
            target.relative_to(self.artifact_root)
        except ValueError as exc:
            raise ValueError("Artifact storage_ref escaped the configured root") from exc
        return target

    async def delete_registered_file(self, storage_ref: str) -> None:
        """删除一条已登记的 Artifact 文件；缺失文件视为已完成，绝不清理父目录。"""

        target = self._resolve_storage_ref(storage_ref)
        await asyncio.to_thread(self._delete_file, target)

    async def read_registered_file(
        self,
        storage_ref: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        """读取一条已登记文件的完整字节；解析和普通文件检查都在受控根目录内完成。"""

        if max_bytes is not None and max_bytes < 0:
            raise ValueError("Artifact 读取上限无效")
        target = self._resolve_storage_ref(storage_ref)
        try:
            return await asyncio.to_thread(self._read_file, target, max_bytes=max_bytes)
        except FileNotFoundError as exc:
            raise ValueError("Artifact 文件不存在") from exc

    @staticmethod
    def _delete_file(target: Path) -> None:
        """仅删除精确普通文件，避免 Session 清理扩大为目录删除。"""

        try:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError("Artifact storage_ref is not a regular file")
            target.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _read_file(target: Path, *, max_bytes: int | None = None) -> bytes:
        """仅允许读取精确普通文件，避免符号链接和目录穿透。"""

        if target.is_symlink() or not target.is_file():
            raise ValueError("Artifact storage_ref is not a regular file")
        with target.open("rb") as handle:
            content = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("Artifact 文件超过登记大小")
        return content

    @staticmethod
    def _write_bytes(target: Path, content: bytes) -> None:
        """先写临时文件再原子替换，避免读取方看到半份 Artifact。"""

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
