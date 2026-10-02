"""阶段五 Run 历史的只读服务。"""

from __future__ import annotations

import hashlib
import json

from application.artifacts import ArtifactStore
from contracts.datalink import DataLinkSemanticContext
from contracts.errors import AppError, ErrorCode
from contracts.runs import (
    RunArtifactRead,
    SqlAuditRead,
    ToolCallRead,
    project_tool_input_params,
)
from contracts.status import ArtifactType, DataSourceStatus
from contracts.trace_dag import TraceDagRead
from contracts.validation import (
    DataLinkConsumptionListRead,
    DataLinkConsumptionRead,
    DataLinkConsumptionSummary,
)
from metadata.models import ArtifactModel, RunModel, SqlAuditLogModel, ToolCallModel
from metadata.repositories import (
    DataSourceRepository,
    RunDatalinkConsumptionRepository,
    RunHistoryRepository,
    RunRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.trace_dag import TraceDagBuilder

MAX_INLINE_FILE_BYTES = 1024 * 1024
_INLINE_FILE_MIME_TYPES = frozenset(
    {
        "application/json",
        "text/csv",
        "text/plain",
        "text/tab-separated-values",
    }
)
_INLINE_MIME_TYPES_BY_ARTIFACT = {
    ArtifactType.TABLE.value: frozenset({"application/json"}),
    ArtifactType.MARKDOWN.value: frozenset({"text/markdown"}),
    ArtifactType.CHART.value: frozenset({"image/png", "image/svg+xml"}),
    ArtifactType.FILE.value: _INLINE_FILE_MIME_TYPES,
}


class RunHistoryService:
    """将 Metadata 投影为安全 DTO；不依赖模型、Data Gateway、DataLink 或 Docker。"""

    def __init__(self, db: AsyncSession, *, artifact_store: ArtifactStore | None = None) -> None:
        self._db = db
        self._runs = RunRepository(db)
        self._history = RunHistoryRepository(db)
        self._datasources = DataSourceRepository(db)
        self._consumptions = RunDatalinkConsumptionRepository(db)
        self._artifact_store = artifact_store

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRead]:
        await self._ensure_run(run_id)
        return [_to_tool_call_read(model) for model in await self._history.list_tool_calls(run_id)]

    async def get_trace_dag(self, run_id: str) -> TraceDagRead:
        """读取当前 Run 的持久化事实并构造确定性 DAG，不触发运行依赖。"""

        run = await self._ensure_run(run_id)
        events = await self._history.list_events(run_id)
        tool_calls = await self._history.list_tool_calls(run_id)
        audits = await self._history.list_sql_audits(run_id)
        artifacts = await self._history.list_artifacts(run_id)
        return TraceDagBuilder().build(
            run=run,
            events=events,
            tool_calls=tool_calls,
            audits=audits,
            artifacts=artifacts,
        )

    async def list_sql_audits(self, run_id: str) -> list[SqlAuditRead]:
        run = await self._ensure_run(run_id)
        datasource_deleted = await self._datasource_deleted(run.datasource_id)
        return [
            _to_sql_audit_read(model, datasource_deleted=datasource_deleted)
            for model in await self._history.list_sql_audits(run_id)
        ]

    async def list_artifacts(self, run_id: str) -> list[RunArtifactRead]:
        run = await self._ensure_run(run_id)
        datasource_deleted = await self._datasource_deleted(run.datasource_id)
        return [
            _to_artifact_read(model, datasource_deleted=datasource_deleted)
            for model in await self._history.list_artifacts(run_id)
        ]

    async def list_datalink_consumptions(self, run_id: str) -> DataLinkConsumptionListRead:
        await self._ensure_run(run_id)
        items = [
            _to_consumption_read(model) for model in await self._consumptions.list_for_run(run_id)
        ]
        return DataLinkConsumptionListRead(
            run_id=run_id,
            historical_status="recorded" if items else "missing",
            items=items,
        )

    async def get_datalink_consumption(
        self, run_id: str, consumption_id: str
    ) -> DataLinkConsumptionRead:
        await self._ensure_run(run_id)
        model = await self._consumptions.get(consumption_id)
        if model is None or model.run_id != run_id:
            raise AppError(ErrorCode.RUN_NOT_FOUND, "DataLink 消费记录不存在", status_code=404)
        return _to_consumption_read(model)

    async def get_artifact(self, artifact_id: str) -> RunArtifactRead:
        model = await self._history.get_artifact(artifact_id)
        if model is None or model.run_id is None:
            raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 不存在", status_code=404)
        run = await self._ensure_run(model.run_id)
        return _to_artifact_read(
            model,
            datasource_deleted=await self._datasource_deleted(run.datasource_id),
        )

    async def read_artifact_content(
        self,
        artifact_id: str,
        *,
        allow_file_download: bool = False,
    ) -> tuple[bytes, str]:
        """读取已登记产物正文；历史读取只访问受控 ArtifactStore，不启动任何运行依赖。"""

        model = await self._get_readable_artifact(artifact_id)
        if not allow_file_download and not _is_inline_previewable(model):
            if model.type == ArtifactType.FILE.value and model.size_bytes > MAX_INLINE_FILE_BYTES:
                raise AppError(
                    ErrorCode.FILE_TOO_LARGE,
                    "Artifact 超出在线预览大小上限",
                    status_code=413,
                )
            raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 仅支持下载", status_code=404)
        full_result = (
            model.metadata_json is not None and model.metadata_json.get("full_result") is True
        )
        if not full_result and model.preview_json is not None:
            content = json.dumps(
                model.preview_json, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if hashlib.sha256(content).hexdigest() != model.content_hash:
                raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 内容不可用", status_code=404)
            return content, "application/json"
        if model.storage_ref is None:
            raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 内容不存在", status_code=404)
        store = self._require_artifact_store()
        try:
            content = await store.read_registered_file(
                model.storage_ref,
                max_bytes=model.size_bytes,
            )
        except ValueError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 内容不存在", status_code=404
            ) from exc
        if (
            len(content) != model.size_bytes
            or hashlib.sha256(content).hexdigest() != model.content_hash
        ):
            raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 内容不可用", status_code=404)
        if not allow_file_download and model.type == ArtifactType.FILE.value:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AppError(
                    ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 文本内容不可用", status_code=404
                ) from exc
        return content, model.mime_type

    async def artifact_download(self, artifact_id: str) -> tuple[bytes, str, str]:
        """准备安全下载响应；文件名只来自固定标题和受控 MIME 类型。"""

        model = await self._get_readable_artifact(artifact_id)
        content, mime_type = await self.read_artifact_content(artifact_id, allow_file_download=True)
        return content, mime_type, _download_filename(model.title, mime_type)

    async def _ensure_run(self, run_id: str) -> RunModel:
        run = await self._runs.get(run_id)
        if run is None:
            raise AppError(ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404)
        return run

    async def _datasource_deleted(self, datasource_id: str | None) -> bool:
        """历史数据只在 tombstone 已落为 deleted 时显示安全删除标记。"""

        if datasource_id is None:
            return False
        datasource = await self._datasources.get(datasource_id)
        return datasource is not None and datasource.status == DataSourceStatus.DELETED.value

    async def _get_readable_artifact(self, artifact_id: str) -> ArtifactModel:
        """确认 Artifact 属于仍可读取的 Run，防止用孤立 Metadata 读文件。"""

        model = await self._history.get_artifact(artifact_id)
        if model is None or model.run_id is None:
            raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact 不存在", status_code=404)
        await self._ensure_run(model.run_id)
        return model

    def _require_artifact_store(self) -> ArtifactStore:
        if self._artifact_store is None:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "Artifact 存储未配置", status_code=500)
        return self._artifact_store


def _to_tool_call_read(model: ToolCallModel) -> ToolCallRead:
    """工具输入先按白名单投影，完整输出仍只保留已经脱敏的摘要。"""

    if not model.tool_call_id:
        raise ValueError("ToolCall 缺少当前协议要求的 tool_call_id")
    summary = model.output_summary_json
    if summary is not None and not all(
        isinstance(value, str | int | float | bool | type(None)) for value in summary.values()
    ):
        summary = None
    return ToolCallRead(
        # API 只暴露模型在当前 Run 内的调用编号；数据库主键不属于协议。
        id=model.tool_call_id,
        run_id=model.run_id,
        tool_name=model.tool_name,
        status=model.status,
        input_params=project_tool_input_params(model.tool_name, model.input_json),
        output_summary=summary,
        error_code=model.error_code,
        error_message=model.error_message,
        started_at=model.started_at,
        finished_at=model.finished_at,
    )


def _to_sql_audit_read(
    model: SqlAuditLogModel, *, datasource_deleted: bool = False
) -> SqlAuditRead:
    """返回已持久化 SQL 与安全审计字段，不构造连接、堆栈或路径。"""

    return SqlAuditRead(
        id=model.id,
        connection_revision=model.connection_revision,
        run_id=model.run_id,
        tool_call_id=model.tool_call_id,
        datasource_id=model.datasource_id,
        datasource_deleted=datasource_deleted,
        schema_revision=model.schema_revision,
        attempt_no=model.attempt_no,
        repaired_from_id=model.repaired_from_id,
        original_sql=model.original_sql,
        normalized_sql=model.normalized_sql,
        status=model.status,
        statement_type=model.statement_type,
        referenced_tables=model.referenced_tables_json or [],
        blocked_reason_code=model.blocked_reason_code,
        blocked_reason=model.blocked_reason,
        artifact_id=model.artifact_id,
        row_count=model.row_count,
        elapsed_ms=model.elapsed_ms,
        error_code=model.error_code,
        error_message=model.error_message,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _to_artifact_read(model: ArtifactModel, *, datasource_deleted: bool = False) -> RunArtifactRead:
    """Artifact 元数据不返回 storage_ref，文件读取必须经过专用受控入口。"""

    return RunArtifactRead(
        id=model.id,
        run_id=model.run_id,
        session_id=model.session_id,
        tool_call_id=model.tool_call_id,
        datasource_deleted=datasource_deleted,
        type=ArtifactType(model.type),
        title=model.title,
        mime_type=model.mime_type,
        size_bytes=model.size_bytes,
        inline_previewable=_is_inline_previewable(model),
        preview=model.preview_json,
        metadata=model.metadata_json,
        content_hash=model.content_hash,
        created_at=model.created_at,
    )


def _to_consumption_read(model) -> DataLinkConsumptionRead:
    context = (
        DataLinkSemanticContext.model_validate(model.payload_json)
        if model.payload_json is not None
        else None
    )
    return DataLinkConsumptionRead(
        id=model.id,
        run_id=model.run_id,
        stage=model.stage,
        seq=model.seq,
        query=model.query,
        focus=model.focus,
        max_nodes=model.max_nodes,
        schema_revision=model.schema_revision,
        graph_version=model.graph_version,
        mode=model.mode,
        payload_status=model.payload_status,
        returned_status=model.returned_status,
        consumer_receipt_status=model.consumer_receipt_status,
        is_truncated=model.is_truncated,
        tool_call_id=model.tool_call_id,
        payload_version=model.payload_version,
        semantic_context=context,
        summary=DataLinkConsumptionSummary.model_validate(model.summary_json),
        created_at=model.created_at,
    )


def _is_inline_previewable(model: ArtifactModel) -> bool:
    """根据已登记类型、MIME、大小和内容引用投影内联读取能力。"""

    allowed_mime_types = _INLINE_MIME_TYPES_BY_ARTIFACT.get(model.type)
    if allowed_mime_types is None or model.mime_type not in allowed_mime_types:
        return False
    if model.type == ArtifactType.FILE.value:
        return model.storage_ref is not None and model.size_bytes <= MAX_INLINE_FILE_BYTES
    if model.type == ArtifactType.TABLE.value:
        return model.storage_ref is not None or model.preview_json is not None
    return model.storage_ref is not None


def _download_filename(title: str, mime_type: str) -> str:
    """生成跨平台安全文件名，避免 Metadata 标题变成响应头注入或路径。"""

    base = "".join(
        character
        if character.isascii() and (character.isalnum() or character in {"_", "-"})
        else "_"
        for character in title
    )
    suffix = {
        "application/json": ".json",
        "text/csv": ".csv",
        "text/tab-separated-values": ".tsv",
        "text/markdown": ".md",
        "text/plain": ".txt",
        "image/png": ".png",
        "image/svg+xml": ".svg",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }.get(mime_type, ".bin")
    return f"{base[:80] or 'artifact'}{suffix}"
