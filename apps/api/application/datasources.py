from __future__ import annotations

import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from contracts.api import Page, PageResult
from contracts.datalink import (
    DataLinkBuildStatus,
    DataLinkCatalogDetailRead,
    DataLinkCatalogRead,
    DataLinkDraftExploreRead,
    DataLinkDraftPreviewRequest,
    DataLinkDraftRead,
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkGraphEntriesRead,
    DataLinkGraphEntryType,
    DataLinkGraphRead,
    DataLinkPublishRead,
    DataLinkPublishRequest,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    DataLinkRelationsRead,
    DataLinkStatusRead,
    DataLinkSubgraphRead,
)
from contracts.datasources import (
    ArtifactUsage,
    DataSourceConnectionCreate,
    DataSourceConnectionUpdate,
    DataSourceDeleteResult,
    DataSourceDescriptionUpdate,
    DataSourceMaskFieldsUpdate,
    DataSourceRead,
    DataSourceTypeDescriptor,
    SchemaSummaryRead,
    SqlExecutionRead,
    TableDataRead,
)
from contracts.errors import AppError, ErrorCode
from contracts.ids import make_id
from contracts.status import DataSourceStatus
from data_gateway.exceptions import DataSourceCheckError
from data_gateway.registry import get_type_descriptor, supported_data_source_types
from data_gateway.service import DataGateway
from data_gateway.types import GatewaySourceSnapshot, QueryCancelToken, SourceAccess
from metadata.connection_repository import ConnectionRepository
from metadata.models import DataSourceModel, RunModel
from metadata.repositories import DataSourceRepository

from application.connections import ConnectionService
from application.datalink_client import (
    DataLinkClientError,
    DataLinkManagementPort,
)
from application.secret_cipher import SecretCipher
from application.source_access import resolve_source_access

if TYPE_CHECKING:
    from runtime.run_lifecycle import RunLifecycleCoordinator

logger = logging.getLogger(__name__)


def to_data_source_read(model: DataSourceModel) -> DataSourceRead:
    """把内部 DataSource 模型转换为不含实际文件路径的 API 投影。"""

    return DataSourceRead(
        id=model.id,
        name=model.name,
        description=model.description,
        type=model.type,
        status=model.status,
        schema_revision=model.schema_revision,
        source_kind=model.source_kind,
        connection_revision=model.connection_revision,
        has_credentials=model.credential_ref is not None,
        connection_summary=model.connection_config_json
        if model.source_kind == "connection"
        else None,
        mask_fields=list(model.mask_fields_json or []),
        mask_fields_confirmed=model.mask_fields_confirmed,
        schema_summary=(
            SchemaSummaryRead.model_validate(model.schema_cache_json)
            if model.schema_cache_json is not None
            else None
        ),
        datalink_build_id=model.datalink_build_id,
        datalink_graph_version=model.datalink_graph_version,
        last_error_code=model.last_error_code,
        last_error_message=model.last_error_message,
        last_test_at=model.last_test_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class DataSourceService:
    """拥有上传文件、DataSource 状态机和受控文件路径校验。"""

    def __init__(
        self,
        repository: DataSourceRepository,
        gateway: DataGateway,
        datalink_client: DataLinkManagementPort,
        *,
        datasource_root: Path,
        max_upload_mb: int,
        run_lifecycle: RunLifecycleCoordinator | None = None,
        cipher: SecretCipher | None = None,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.datalink_client = datalink_client
        self.datasource_root = datasource_root.resolve()
        self.max_upload_bytes = max_upload_mb * 1024 * 1024
        self.run_lifecycle = run_lifecycle
        self.cipher = cipher

    def connection_service(self) -> ConnectionService:
        if self.cipher is None:
            raise AppError(ErrorCode.DATASOURCE_CHECK_FAILED, "连接凭据服务不可用", status_code=503)
        return ConnectionService(self.repository, self.cipher)

    async def resolve_source_access(self, model: DataSourceModel) -> SourceAccess:
        return await resolve_source_access(
            self.repository.db, self.cipher, model, self.datasource_root
        )

    async def create_connection(self, payload: DataSourceConnectionCreate) -> DataSourceRead:
        return to_data_source_read(await self.connection_service().create(payload))

    async def update_connection(
        self, datasource_id: str, payload: DataSourceConnectionUpdate
    ) -> DataSourceRead:
        model = await self._get_readable_model(datasource_id)
        return to_data_source_read(await self.connection_service().update(model, payload))

    def supported_types(self) -> list[DataSourceTypeDescriptor]:
        """返回前端可选的数据源类型，类型清单只由网关注册表维护。"""

        return supported_data_source_types()

    async def upload(
        self,
        *,
        name: str,
        description: str | None,
        type_name: str,
        original_filename: str | None,
        content: BinaryIO,
    ) -> DataSourceRead:
        """校验用户选择的类型后保存受控文件，并创建等待检查的 DataSource。"""

        descriptor = get_type_descriptor(type_name)
        extension = Path(original_filename or "").suffix.casefold()
        if extension not in descriptor.accepted_extensions:
            raise AppError(
                ErrorCode.UNSUPPORTED_FILE_TYPE,
                "不支持的文件类型",
                status_code=422,
            )

        datasource_id = make_id("datasource")
        stored_filename = "source.csv" if descriptor.type.value == "csv" else "source.sqlite"
        source_ref = f"{datasource_id}/{stored_filename}"
        source_path = self._source_path(source_ref, require_exists=False)
        try:
            file_size, content_hash = self._write_upload(content, source_path)
            model = await self.repository.create(
                datasource_id=datasource_id,
                name=name,
                description=description,
                datasource_type=descriptor.type,
                source_ref=source_ref,
                file_size=file_size,
                content_hash=content_hash,
            )
        except Exception:
            self._remove_new_upload(source_path)
            raise
        return to_data_source_read(model)

    async def commit(self) -> None:
        """在调度后台检查前提交 inspecting 状态，确保新会话可读取该 DataSource。"""

        await self.repository.db.commit()

    async def list(self, page: Page) -> PageResult[DataSourceRead]:
        """按更新时间倒序返回数据源列表，不暴露受控文件的实际路径。"""

        items, total = await self.repository.list(offset=page.offset, limit=page.page_size)
        return PageResult(
            items=[to_data_source_read(item) for item in items],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def get(self, datasource_id: str) -> DataSourceRead:
        """读取单个数据源的当前状态及已保存的 Schema 摘要。"""

        return to_data_source_read(await self._get_model(datasource_id))

    async def get_readable(self, datasource_id: str) -> DataSourceRead:
        """Return source metadata after the shared readable-state checks."""

        return to_data_source_read(await self._get_readable_model(datasource_id))

    async def get_schema(self, datasource_id: str) -> SchemaSummaryRead:
        """返回检查完成时固化的 Schema，而非再次直接读取上传文件。"""

        model = await self._get_model(datasource_id)
        if model.schema_cache_json is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未生成可用 Schema",
                status_code=409,
            )
        return SchemaSummaryRead.model_validate(model.schema_cache_json)

    async def retry(self, datasource_id: str) -> DataSourceRead:
        """将未删除的数据源重新置为检查中，实际检查由后台任务执行。"""

        model = await self._get_readable_model(datasource_id)
        model = await self.repository.start_inspection(model)
        return to_data_source_read(model)

    async def inspect(self, datasource_id: str, inspection_id: str | None = None) -> None:
        """执行检查并生成 Schema；DataLink 只在用户主动请求后异步建图。"""

        model = await self.repository.get(datasource_id)
        if model is None or model.status in {
            DataSourceStatus.DELETING.value,
            DataSourceStatus.DELETED.value,
        }:
            return
        if inspection_id is not None and model.inspection_id != inspection_id:
            return
        expected = {
            "expected_inspection_id": model.inspection_id,
            "expected_connection_revision": model.connection_revision,
        }
        try:
            schema = await self.gateway.inspect(model, await self.resolve_source_access(model))
        except DataSourceCheckError as exc:
            await self.repository.fail_inspection(
                model, code=exc.code, message=exc.message, **expected
            )
        except Exception:
            logger.error("DataSource inspection failed", extra={"datasource_id": datasource_id})
            await self.repository.fail_inspection(
                model,
                code=ErrorCode.DATASOURCE_CHECK_FAILED.value,
                message="数据源检查失败",
                **expected,
            )
        else:
            inspected = await self.repository.complete_inspection(model, schema, **expected)
            # Schema 就绪即可分析。DataLink 只有用户从页面明确点击时才构建。
            del inspected

    async def preview(self, datasource_id: str, table_name: str, limit: int) -> TableDataRead:
        """通过 Data Gateway 读取白名单表的预览，不能绕过脱敏和状态校验。"""

        model = await self._get_readable_model(datasource_id)
        return await self.gateway.preview(
            model,
            await self.resolve_source_access(model),
            table_name,
            limit,
        )

    async def run_agent_sql(
        self,
        *,
        datasource_id: str,
        run_id: str,
        session_id: str,
        sql: str,
        cancel_token: QueryCancelToken,
        timeout_seconds: float | None = None,
        max_rows: int | None = None,
        artifact_usage: ArtifactUsage = "query_result",
        input_snapshot_path: Path | None = None,
        tool_call_id: str | None = None,
        repaired_from_audit_id: str | None = None,
        source_snapshot: GatewaySourceSnapshot | None = None,
        source_access: SourceAccess | None = None,
    ) -> SqlExecutionRead:
        """为阶段四 Graph 复用唯一 Data Gateway 查询链，不暴露文件路径。"""

        model = await self._get_readable_model(datasource_id)
        query_source = model
        if source_snapshot is not None:
            run = await self.repository.db.get(RunModel, run_id)
            if (
                run is None
                or run.session_id != session_id
                or run.datasource_id != datasource_id
                or source_snapshot.id != datasource_id
                or run.schema_revision != source_snapshot.schema_revision
                or run.connection_revision != source_snapshot.connection_revision
                or source_access is None
            ):
                raise AppError(ErrorCode.HEAD_STALE, "运行数据源快照不一致", status_code=409)
            query_source = source_snapshot
        if (
            query_source.status
            not in {
                DataSourceStatus.SCHEMA_READY.value,
                DataSourceStatus.READY.value,
            }
            or not query_source.mask_fields_confirmed
        ):
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未完成检查或未确认遮蔽字段",
                status_code=409,
            )
        return await self.gateway.run_sql_readonly(
            query_source,
            source_access or input_snapshot_path or await self.resolve_source_access(model),
            sql,
            run_id=run_id,
            session_id=session_id,
            cancel_token=cancel_token,
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
            artifact_usage=artifact_usage,
            tool_call_id=tool_call_id,
            repaired_from_audit_id=repaired_from_audit_id,
        )

    async def run_readonly_sql(
        self,
        *,
        datasource_id: str,
        sql: str,
        cancel_token: QueryCancelToken,
        timeout_seconds: float | None = None,
        expected_schema_revision: int | None = None,
        expected_connection_revision: int | None = None,
        expected_graph_version: str | None = None,
    ) -> SqlExecutionRead:
        """Execute a service-generated read-only query without creating a Run."""

        model = await self._get_readable_model(datasource_id)
        await self.repository.db.refresh(model)
        if (
            model.status
            not in {
                DataSourceStatus.SCHEMA_READY.value,
                DataSourceStatus.READY.value,
            }
            or not model.mask_fields_confirmed
        ):
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未完成检查或未确认遮蔽字段",
                status_code=409,
            )
        if (
            expected_schema_revision is not None
            and model.schema_revision != expected_schema_revision
        ):
            raise AppError(ErrorCode.HEAD_STALE, "数据源版本已变化，请刷新后重试", status_code=409)
        if (
            expected_connection_revision is not None
            and model.connection_revision != expected_connection_revision
        ) or (
            expected_graph_version is not None
            and model.datalink_graph_version != expected_graph_version
        ):
            raise AppError(ErrorCode.HEAD_STALE, "数据源版本已变化，请刷新后重试", status_code=409)
        return await self.gateway.run_sql_readonly(
            model,
            await self.resolve_source_access(model),
            sql,
            cancel_token=cancel_token,
            timeout_seconds=timeout_seconds,
        )

    async def update_mask_fields(
        self,
        datasource_id: str,
        payload: DataSourceMaskFieldsUpdate,
    ) -> DataSourceRead:
        """保存用户确认的遮蔽列；不尝试从字段名或样例自动猜测。"""

        model = await self._get_readable_model(datasource_id)
        try:
            await self.repository.update_mask_fields(model, mask_fields=payload.mask_fields)
        except ValueError as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "遮蔽列必须来自当前 Schema",
                status_code=422,
            ) from exc
        return to_data_source_read(model)

    async def update_description(
        self,
        datasource_id: str,
        payload: DataSourceDescriptionUpdate,
    ) -> DataSourceRead:
        """保存数据源补充说明；不改文件、Schema、遮蔽字段或 DataLink 版本。"""

        model = await self._get_readable_model(datasource_id)
        cleaned = payload.description.strip() if payload.description else None
        if not cleaned:
            cleaned = None
        await self.repository.update_description(model, description=cleaned)
        return to_data_source_read(model)

    async def delete(self, datasource_id: str) -> DataSourceDeleteResult:
        """先写 deleting 并收尾活跃 Run，再清理图谱，保留审计和产物历史。"""

        model = await self._get_model(datasource_id)
        if model.status == DataSourceStatus.DELETED.value:
            return DataSourceDeleteResult(
                datasource_id=model.id,
                status=DataSourceStatus(model.status),
            )

        model = await self.repository.start_deletion(model)
        await ConnectionRepository(self.repository.db).revoke(model.id)
        await self.commit()
        if self.run_lifecycle is not None:
            await self.run_lifecycle.cancel_for_datasource(
                model.id,
                reason="datasource_deleted",
            )
        try:
            removed = await self.datalink_client.remove(model.id)
            if removed.datasource_id != model.id:
                raise self._invalid_datalink_result()
        except DataLinkClientError as exc:
            await self.repository.fail_deletion(model, code=exc.code.value, message=exc.message)
            await self.commit()
            raise self._to_app_error(exc) from exc

        if model.credential_ref is not None:
            from metadata.repositories import SecretRepository

            secret_ref = model.credential_ref
            model.credential_ref = None
            await self.repository.db.flush()
            await SecretRepository(self.repository.db).delete_by_id(secret_ref)
        model = await self.repository.complete_deletion(model)
        return DataSourceDeleteResult(datasource_id=model.id, status=DataSourceStatus(model.status))

    async def rebuild_datalink(self, datasource_id: str) -> DataLinkRebuildResult:
        """认领后台重建；完成版本仍必须经状态对账后才发布到 DataSource。"""

        model = await self._get_readable_model(datasource_id)
        if model.source_kind == "connection" and model.status not in {
            "schema_ready",
            "ready",
            "building_datalink",
        }:
            raise AppError(ErrorCode.DATASOURCE_NOT_READY, "数据源尚未完成检查", status_code=409)
        if model.schema_cache_json is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未生成可用 Schema",
                status_code=409,
            )
        return await self._rebuild_started_model(model, raise_on_failure=True)

    async def datalink_status(self, datasource_id: str) -> DataLinkStatusRead:
        """读取 DataLink 状态，并在超时或重启后把已完成 Build 投影回主后端。"""

        model = await self._get_readable_model(datasource_id)
        try:
            status = await self.datalink_client.status(model.id)
        except DataLinkClientError as exc:
            if exc.code == DataLinkErrorCode.DATALINK_UNAVAILABLE:
                await self.repository.note_datalink_unavailable(model, message=exc.message)
            else:
                await self.repository.fail_datalink_build(
                    model,
                    code=exc.code.value,
                    message=exc.message,
                )
            # 此处会抛出 AppError；先提交才能避免请求依赖的回滚吞掉待对账错误。
            await self.commit()
            raise self._to_app_error(exc) from exc
        await self._reconcile_datalink_status(model, status)
        return status

    async def datalink_graph(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
    ) -> DataLinkGraphRead:
        """经主后端转发受限图谱面板数据，前端不直接接触 DataLink 端口。"""

        model = await self._get_readable_model(datasource_id)
        resolved_version = self._require_datalink_graph_version(model, graph_version)
        try:
            return await self.datalink_client.graph(
                model.id,
                graph_version=resolved_version,
            )
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_graph_entries(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        entry_type: DataLinkGraphEntryType | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkGraphEntriesRead:
        """经主后端读取版本化图谱入口，页面不能自行枚举 DataLink 内容。"""

        model = await self._get_readable_model(datasource_id)
        resolved_version = self._require_datalink_graph_version(model, graph_version)
        try:
            return await self.datalink_client.graph_entries(
                model.id,
                graph_version=resolved_version,
                entry_type=entry_type,
                query=query,
                page=page,
                page_size=page_size,
            )
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_subgraph(
        self,
        datasource_id: str,
        *,
        root_node_id: str,
        graph_version: str | None = None,
        edge_types: list[DataLinkEdgeType] | None = None,
        hops: int = 1,
    ) -> DataLinkSubgraphRead:
        """经主后端读取一个完成版本中受限的一到两跳局部图。"""

        model = await self._get_readable_model(datasource_id)
        resolved_version = self._require_datalink_graph_version(model, graph_version)
        try:
            return await self.datalink_client.graph_subgraph(
                model.id,
                root_node_id=root_node_id,
                graph_version=resolved_version,
                edge_types=edge_types,
                hops=hops,
            )
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_catalog(self, datasource_id: str, **kwargs) -> DataLinkCatalogRead:
        model = await self._get_readable_model(datasource_id)
        kwargs["graph_version"] = self._require_datalink_graph_version(
            model, kwargs.get("graph_version")
        )
        try:
            return await self.datalink_client.catalog(model.id, **kwargs)
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_catalog_detail(
        self, datasource_id: str, node_id: str, **kwargs
    ) -> DataLinkCatalogDetailRead:
        model = await self._get_readable_model(datasource_id)
        kwargs["graph_version"] = self._require_datalink_graph_version(
            model, kwargs.get("graph_version")
        )
        try:
            return await self.datalink_client.catalog_detail(model.id, node_id, **kwargs)
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_relations(self, datasource_id: str, **kwargs) -> DataLinkRelationsRead:
        model = await self._get_readable_model(datasource_id)
        kwargs["graph_version"] = self._require_datalink_graph_version(
            model, kwargs.get("graph_version")
        )
        try:
            return await self.datalink_client.relations(model.id, **kwargs)
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_draft(self, datasource_id: str) -> DataLinkDraftRead | None:
        model = await self._get_readable_model(datasource_id)
        try:
            return await self.datalink_client.draft(model.id)
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def save_datalink_draft(
        self, datasource_id: str, payload: DataLinkDraftSaveRequest
    ) -> DataLinkDraftRead:
        model = await self._get_readable_model(datasource_id)
        if payload.schema_revision != model.schema_revision:
            raise AppError(ErrorCode.INVALID_QUERY, "草稿的 Schema 版本已过期", status_code=409)
        try:
            return await self.datalink_client.save_draft(model.id, payload)
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def datalink_draft_preview(
        self, datasource_id: str, payload: DataLinkDraftPreviewRequest
    ) -> DataLinkDraftExploreRead:
        model = await self._get_readable_model(datasource_id)
        try:
            result = await self.datalink_client.draft_preview(model.id, payload)
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc
        if result.schema_revision != model.schema_revision:
            raise AppError(ErrorCode.HEAD_STALE, "草稿的 Schema 版本已过期", status_code=409)
        return result

    async def publish_datalink(
        self, datasource_id: str, payload: DataLinkPublishRequest
    ) -> DataLinkPublishRead:
        model = await self._get_readable_model(datasource_id)
        try:
            draft = await self.datalink_client.draft(model.id)
            if draft is None or draft.schema_revision != model.schema_revision:
                raise AppError(
                    ErrorCode.INVALID_QUERY, "草稿不存在或 Schema 已变化", status_code=409
                )
            result = await self.datalink_client.publish(model.id, payload)
            status = await self.datalink_client.status(model.id)
            await self._reconcile_datalink_status(model, status)
            return result
        except DataLinkClientError as exc:
            raise self._to_app_error(exc) from exc

    async def recover_interrupted_datalink_builds(self) -> None:
        """主后端重启时向 DataLink status 对账，不自行猜测 running Build 的最终结果。"""

        for model in await self.repository.list_building_datalink():
            try:
                status = await self.datalink_client.status(model.id)
            except DataLinkClientError as exc:
                if exc.code == DataLinkErrorCode.DATALINK_UNAVAILABLE:
                    await self.repository.note_datalink_unavailable(model, message=exc.message)
                else:
                    await self.repository.fail_datalink_build(
                        model,
                        code=exc.code.value,
                        message=exc.message,
                    )
            else:
                await self._reconcile_datalink_status(model, status)

    @staticmethod
    def _require_datalink_graph_version(
        model: DataSourceModel,
        graph_version: str | None,
    ) -> str:
        """没有主后端确认版本时拒绝浏览，固定版本仍由 DataLink 校验归属和完成状态。"""

        resolved_version = graph_version or model.datalink_graph_version
        if resolved_version is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未确认可用的 DataLink 图谱版本",
                status_code=409,
            )
        return resolved_version

    async def _get_model(self, datasource_id: str) -> DataSourceModel:
        """获取 Metadata 模型；不存在时转成对外稳定的 404 错误。"""

        model = await self.repository.get(datasource_id)
        if model is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_FOUND,
                "数据源不存在",
                status_code=404,
                details={"datasource_id": datasource_id},
            )
        return model

    async def _get_readable_model(self, datasource_id: str) -> DataSourceModel:
        """拒绝已经进入删除流程的数据源，避免 tombstone 被重新使用。"""

        model = await self._get_model(datasource_id)
        if model.status in {DataSourceStatus.DELETING.value, DataSourceStatus.DELETED.value}:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源已删除，不能再读取",
                status_code=409,
            )
        return model

    def _source_path_for_model(self, model: DataSourceModel) -> Path:
        """从 Metadata 的相对引用解析上传文件，并要求它仍是普通文件。"""

        if model.source_ref is None:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件不存在")
        return self._source_path(model.source_ref, require_exists=True)

    def _source_path(self, source_ref: str, *, require_exists: bool) -> Path:
        """把相对 source_ref 限制在 datasource_root 内，防止路径穿越和符号链接。"""

        relative = PurePosixPath(source_ref)
        if relative.is_absolute() or ".." in relative.parts or "\\" in source_ref:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件引用无效")
        target = (self.datasource_root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(self.datasource_root)
        except ValueError as exc:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件引用无效") from exc
        if require_exists and (not target.is_file() or target.is_symlink()):
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件不存在")
        return target

    async def _rebuild_started_model(
        self,
        model: DataSourceModel,
        *,
        raise_on_failure: bool,
    ) -> DataLinkRebuildResult:
        """初次建图先持久化 building；已有图谱的后台重建保留 ready 投影。"""

        building = await self.repository.start_datalink_build(model)
        if building.status not in {
            DataSourceStatus.BUILDING_DATALINK.value,
            DataSourceStatus.READY.value,
        }:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源正在删除，不能构建 DataLink",
                status_code=409,
            )
        # 初次建图必须先可见；进程在等待期间退出后，启动恢复才能准确对账。
        if building.status == DataSourceStatus.BUILDING_DATALINK.value:
            await self.commit()
        rebuild_key = make_id("rebuild")
        source_kind = getattr(
            building,
            "source_kind",
            "connection" if building.source_ref is None else "file",
        )
        connection_revision = getattr(building, "connection_revision", 0)
        if source_kind == "connection":
            grant = await self.connection_service().issue_grant(building, rebuild_key)
            await self.commit()
            request = DataLinkRebuildRequest(
                datasource_id=building.id,
                rebuild_key=rebuild_key,
                source_type=building.type,
                source_kind="connection",
                connection_grant=grant,
                connection_revision=connection_revision,
                schema_revision=building.schema_revision,
            )
        else:
            request = DataLinkRebuildRequest(
                datasource_id=building.id,
                rebuild_key=rebuild_key,
                source_type=building.type,
                source_ref=building.source_ref,
                connection_revision=0,
                schema_revision=building.schema_revision,
            )
        try:
            result = await self.datalink_client.rebuild(request)
            if result.status == DataLinkBuildStatus.RUNNING:
                self._validate_running_rebuild_result(building, result)
                return result
            self._validate_rebuild_result(building, result)
            if result.publication_state == "candidate":
                status = await self.datalink_client.status(building.id)
                await self._reconcile_datalink_status(building, status)
                return result
        except DataLinkClientError as exc:
            if exc.code == DataLinkErrorCode.DATALINK_UNAVAILABLE:
                await self.repository.note_datalink_unavailable(building, message=exc.message)
            elif exc.code == DataLinkErrorCode.BUILD_ALREADY_RUNNING:
                # 同一 DataSource 的活动 Build 已由 DataLink 持久化；本地投影保持等待状态。
                pass
            else:
                await self.repository.fail_datalink_build(
                    building,
                    code=exc.code.value,
                    message=exc.message,
                )
            if raise_on_failure:
                await self.commit()
                raise self._to_app_error(exc) from exc
            fallback_build_id = exc.details.get(
                "build_id", building.datalink_build_id or "build_unavailable"
            )
            return DataLinkRebuildResult(
                build_id=fallback_build_id,
                datasource_id=building.id,
                status=(
                    DataLinkBuildStatus.RUNNING
                    if exc.code == DataLinkErrorCode.BUILD_ALREADY_RUNNING
                    else DataLinkBuildStatus.FAILED
                ),
                requested_schema_revision=building.schema_revision,
                connection_revision=connection_revision,
            )

        completion = {
            "build_id": result.build_id,
            "graph_version": result.graph_version or "",
            "expected_schema_revision": result.requested_schema_revision,
        }
        if result.connection_revision:
            completion["expected_connection_revision"] = result.connection_revision
        await self.repository.complete_datalink_build(building, **completion)
        return result

    async def _reconcile_datalink_status(
        self,
        model: DataSourceModel,
        status: DataLinkStatusRead,
    ) -> None:
        """收敛初次建图或现有图谱重建，防止页面刷新覆盖删除状态。"""

        if model.status not in {
            DataSourceStatus.BUILDING_DATALINK.value,
            DataSourceStatus.READY.value,
            DataSourceStatus.FAILED.value,
            DataSourceStatus.SCHEMA_READY.value,
        }:
            return
        build = status.current_build
        if build is None:
            await self.repository.fail_datalink_build(
                model,
                code=DataLinkErrorCode.BUILD_FAILED.value,
                message="DataLink 未找到对应的构建记录",
            )
            return
        if (
            build.datasource_id != model.id
            or build.schema_revision != model.schema_revision
            or build.connection_revision != getattr(model, "connection_revision", 0)
        ):
            await self.repository.fail_datalink_build(
                model,
                code=DataLinkErrorCode.BUILD_FAILED.value,
                message="DataLink 返回的构建版本与数据源不一致",
            )
            return
        published = status.head_build
        if published is None and build.graph_version == status.current_graph_version:
            published = build
        if (
            published is not None
            and published.datasource_id == model.id
            and published.schema_revision == model.schema_revision
            and published.connection_revision == getattr(model, "connection_revision", 0)
            and published.status == DataLinkBuildStatus.COMPLETED
            and published.publication_state == "published"
            and published.graph_version == status.current_graph_version
            and published.graph_version is not None
        ):
            completion = {
                "build_id": published.build_id,
                "graph_version": published.graph_version,
                "expected_schema_revision": published.schema_revision,
            }
            if published.connection_revision:
                completion["expected_connection_revision"] = published.connection_revision
            await self.repository.complete_datalink_build(model, **completion)
            if build.status == DataLinkBuildStatus.COMPLETED:
                return
        if build.status == DataLinkBuildStatus.RUNNING:
            return
        await self.repository.fail_datalink_build(
            model,
            code=(build.error_code or DataLinkErrorCode.BUILD_FAILED).value,
            message=build.error_message or "DataLink 构建失败",
        )

    @staticmethod
    def _validate_rebuild_result(
        model: DataSourceModel,
        result: DataLinkRebuildResult,
    ) -> None:
        """主后端只接受本数据源、本 Schema revision 的完成版本，避免错误投影。"""

        if (
            result.status != DataLinkBuildStatus.COMPLETED
            or result.datasource_id != model.id
            or result.requested_schema_revision != model.schema_revision
            or result.connection_revision != getattr(model, "connection_revision", 0)
            or not result.graph_version
        ):
            raise DataSourceService._invalid_datalink_result()

    @staticmethod
    def _validate_running_rebuild_result(
        model: DataSourceModel,
        result: DataLinkRebuildResult,
    ) -> None:
        """运行中响应只能确认认领成功，候选版本不得提前进入主后端。"""

        if (
            result.datasource_id != model.id
            or result.requested_schema_revision != model.schema_revision
            or result.connection_revision != getattr(model, "connection_revision", 0)
            or result.graph_version is not None
        ):
            raise DataSourceService._invalid_datalink_result()

    @staticmethod
    def _invalid_datalink_result() -> DataLinkClientError:
        """将不符合跨进程契约的结果收敛成稳定失败，不暴露原始响应。"""

        return DataLinkClientError(
            DataLinkErrorCode.BUILD_FAILED,
            "DataLink 返回的构建结果不完整或版本不一致",
            status_code=502,
        )

    @staticmethod
    def _to_app_error(exc: DataLinkClientError) -> AppError:
        """将独立服务错误映射为主后端 API 的统一错误信封。"""

        status_code = 503 if exc.code == DataLinkErrorCode.DATALINK_UNAVAILABLE else exc.status_code
        if exc.code == DataLinkErrorCode.PATH_OUTSIDE_ROOT:
            status_code = 400
        elif exc.code in {
            DataLinkErrorCode.BUILD_ALREADY_RUNNING,
            DataLinkErrorCode.BUILD_FAILED,
            DataLinkErrorCode.BUILD_INTERRUPTED,
            DataLinkErrorCode.MODEL_CONFIG_INVALID,
            DataLinkErrorCode.MODEL_REQUEST_TIMEOUT,
            DataLinkErrorCode.MODEL_UPSTREAM_ERROR,
            DataLinkErrorCode.MODEL_RESPONSE_ERROR,
            DataLinkErrorCode.MODEL_RESPONSE_INVALID,
            DataLinkErrorCode.SEMANTIC_MAPPING_INVALID,
            DataLinkErrorCode.DATASOURCE_MISMATCH,
            DataLinkErrorCode.INVALID_QUERY,
        }:
            status_code = 409
        return AppError(
            ErrorCode(exc.code.value),
            exc.message,
            status_code=status_code,
            details=exc.details,
        )

    def _write_upload(self, content: BinaryIO, target: Path) -> tuple[int, str]:
        """流式落盘并计算 SHA-256；超过大小限制时清理未完成的临时文件。"""

        target.parent.mkdir(parents=True, exist_ok=False)
        temporary = target.with_suffix(f"{target.suffix}.uploading")
        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("xb") as destination:
                while chunk := content.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise AppError(
                            ErrorCode.FILE_TOO_LARGE,
                            "文件超过 100 MB 限制",
                            status_code=413,
                        )
                    digest.update(chunk)
                    destination.write(chunk)
            temporary.replace(target)
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise
        return total, digest.hexdigest()

    @staticmethod
    def _remove_new_upload(target: Path) -> None:
        """仅在 Metadata 创建失败时回收本次刚写入的文件和空目录。"""

        if target.exists():
            target.unlink()
        if target.parent.exists():
            target.parent.rmdir()
