from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from contracts.datasources import SchemaSummaryRead
from contracts.ids import make_id
from contracts.model_profiles import (
    FinalOutputMode,
    ModelProfileCapabilities,
    ModelProfileCreate,
    ModelProfileUpdate,
)
from contracts.runs import RunProtocolId
from contracts.sessions import SessionCreate, SessionUpdate
from contracts.status import (
    DataSourceStatus,
    DataSourceType,
    MessageRole,
    ModelProfileStatus,
    RunStatus,
)
from contracts.validation import DataLinkConsumptionCreate, DataLinkValidationRequest
from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from metadata.models import (
    ArtifactCleanupTaskModel,
    ArtifactModel,
    DataLinkValidationModel,
    DatasourceConversationStateModel,
    DataSourceModel,
    HistoricalAnswerSummaryModel,
    MessageModel,
    ModelProfileModel,
    RunDatalinkConsumptionModel,
    RunEventModel,
    RunModel,
    SecretModel,
    SessionModel,
    SessionPreferencesModel,
    SqlAuditLogModel,
    ToolCallModel,
)

_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user search cannot match the whole table."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_contains(column: ColumnElement[str], value: str) -> ColumnElement[bool]:
    return column.like(f"%{_escape_like(value)}%", escape=_LIKE_ESCAPE)


class SessionRepository:
    """封装会话的 Metadata 读写，不包含消息或 Run 的业务规则。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, payload: SessionCreate) -> SessionModel:
        model = SessionModel(
            id=make_id("session"),
            title=payload.title,
            selected_datasource_id=payload.selected_datasource_id,
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def list(
        self, *, offset: int, limit: int, q: str | None = None
    ) -> tuple[list[SessionModel], int]:
        count_stmt = select(func.count()).select_from(SessionModel)
        stmt = select(SessionModel)
        if q:
            match = _like_contains(SessionModel.title, q)
            count_stmt = count_stmt.where(match)
            stmt = stmt.where(match)
        total = await self.db.scalar(count_stmt)
        result = await self.db.scalars(
            stmt.order_by(SessionModel.updated_at.desc(), SessionModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), int(total or 0)

    async def get(self, session_id: str) -> SessionModel | None:
        return await self.db.get(SessionModel, session_id)

    async def get_for_update(self, session_id: str) -> SessionModel | None:
        """在创建 Run 的事务中锁定 Session，串行化消息 high-water 分配。"""

        result = await self.db.scalars(
            select(SessionModel).where(SessionModel.id == session_id).with_for_update()
        )
        model = result.first()
        if model is not None:
            # SQLite ignores FOR UPDATE; a no-op UPDATE still acquires a
            # write reservation before reading the high-water mark.
            await self.db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(updated_at=SessionModel.updated_at)
            )
            await self.db.refresh(model)
        return model

    async def update(self, model: SessionModel, payload: SessionUpdate) -> SessionModel:
        changes = payload.model_dump(exclude_unset=True)
        for key, value in changes.items():
            setattr(model, key, value)
        await self.db.flush()
        return model

    async def delete(self, model: SessionModel) -> None:
        await self.db.delete(model)
        await self.db.flush()


class DataSourceRepository:
    """维护 DataSource 的状态、Schema 快照和删除 tombstone。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        datasource_id: str,
        name: str,
        description: str | None,
        datasource_type: DataSourceType,
        source_ref: str,
        file_size: int,
        content_hash: str,
        mask_fields: list[str] | None = None,
        mask_fields_confirmed: bool = False,
    ) -> DataSourceModel:
        model = DataSourceModel(
            id=datasource_id,
            name=name,
            description=description,
            type=datasource_type.value,
            source_ref=source_ref,
            file_size=file_size,
            content_hash=content_hash,
            mask_fields_json=sorted(set(mask_fields or [])),
            mask_fields_confirmed=mask_fields_confirmed,
            status=DataSourceStatus.INSPECTING.value,
            inspection_id=make_id("inspection"),
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def update_mask_fields(
        self,
        model: DataSourceModel,
        *,
        mask_fields: list[str],
    ) -> DataSourceModel:
        """保存数据源所有者确认的遮蔽列，不根据样例或列名自动推断。"""

        schema_columns = {
            column["name"]
            for table in (model.schema_cache_json or {}).get("tables", [])
            for column in table.get("columns", [])
            if isinstance(column, dict) and isinstance(column.get("name"), str)
        }
        normalized_schema_columns = {column.casefold() for column in schema_columns}
        unknown = sorted(
            {field for field in mask_fields if field.casefold() not in normalized_schema_columns}
        )
        if unknown:
            raise ValueError("mask_fields contains columns outside the saved Schema")
        model.mask_fields_json = sorted(set(mask_fields))
        model.mask_fields_confirmed = True
        await self.db.flush()
        return model

    async def update_description(
        self,
        model: DataSourceModel,
        *,
        description: str | None,
    ) -> DataSourceModel:
        """只改补充说明，不触碰文件、Schema、遮蔽字段或 DataLink 版本。"""

        model.description = description
        await self.db.flush()
        return model

    async def list(self, *, offset: int, limit: int) -> tuple[list[DataSourceModel], int]:
        total = await self.db.scalar(select(func.count()).select_from(DataSourceModel))
        result = await self.db.scalars(
            select(DataSourceModel)
            .order_by(DataSourceModel.updated_at.desc(), DataSourceModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), int(total or 0)

    async def get(self, datasource_id: str) -> DataSourceModel | None:
        return await self.db.get(DataSourceModel, datasource_id)

    async def start_inspection(self, model: DataSourceModel) -> DataSourceModel:
        """开始一次新检查，并清除上次失败留下的安全错误。"""

        await self.db.execute(
            update(DataSourceModel)
            .where(
                DataSourceModel.id == model.id,
                DataSourceModel.status.not_in(("deleting", "deleted")),
            )
            .values(
                status="inspecting",
                inspection_id=make_id("inspection"),
                last_error_code=None,
                last_error_message=None,
            )
        )
        await self.db.refresh(model)
        return model

    async def complete_inspection(
        self,
        model: DataSourceModel,
        schema: SchemaSummaryRead,
        *,
        expected_inspection_id: str | None = None,
        expected_connection_revision: int | None = None,
    ) -> DataSourceModel:
        """保存最新 Schema；只有结构变化时才递增 schema_revision。"""

        inspection_id = (
            expected_inspection_id if expected_inspection_id is not None else model.inspection_id
        )
        connection_revision = (
            expected_connection_revision
            if expected_connection_revision is not None
            else model.connection_revision
        )
        await self.db.refresh(model)
        if (
            model.status != DataSourceStatus.INSPECTING.value
            or model.inspection_id != inspection_id
            or model.connection_revision != connection_revision
        ):
            return model
        schema_data = schema.model_dump(mode="json")
        previous = model.schema_cache_json
        schema_changed = previous is not None and _schema_structure(previous) != _schema_structure(
            schema_data
        )
        if previous is None:
            model.schema_revision = 1
        elif schema_changed:
            model.schema_revision += 1
        if schema_changed:
            # Schema 变化后旧图谱版本不再能代表当前 DataSource，不能继续投影为可用版本。
            model.datalink_build_id = None
            model.datalink_graph_version = None
            model.mask_fields_confirmed = False
            valid_columns = {
                column.name.casefold() for table in schema.tables for column in table.columns
            }
            model.mask_fields_json = [
                field
                for field in (model.mask_fields_json or [])
                if field.casefold() in valid_columns
            ]
        model.schema_cache_json = schema_data
        model.status = DataSourceStatus.SCHEMA_READY.value
        model.last_error_code = None
        model.last_error_message = None
        model.last_test_at = datetime.now(UTC)
        await self.db.flush()
        return model

    async def fail_inspection(
        self,
        model: DataSourceModel,
        *,
        code: str,
        message: str,
        expected_inspection_id: str | None = None,
        expected_connection_revision: int | None = None,
    ) -> DataSourceModel:
        """记录检查失败；若数据源已被删除，则保留删除状态优先级。"""

        inspection_id = (
            expected_inspection_id if expected_inspection_id is not None else model.inspection_id
        )
        connection_revision = (
            expected_connection_revision
            if expected_connection_revision is not None
            else model.connection_revision
        )
        await self.db.refresh(model)
        if (
            model.status != DataSourceStatus.INSPECTING.value
            or model.inspection_id != inspection_id
            or model.connection_revision != connection_revision
        ):
            return model
        model.status = DataSourceStatus.FAILED.value
        model.last_error_code = code
        model.last_error_message = message
        await self.db.flush()
        return model

    async def recover_interrupted_inspections(self) -> int:
        """服务启动时把遗留 inspecting 记录置为可见的失败状态，不自动重试。"""

        result = await self.db.execute(
            update(DataSourceModel)
            .where(DataSourceModel.status == DataSourceStatus.INSPECTING.value)
            .values(
                status=DataSourceStatus.FAILED.value,
                last_error_code="DATASOURCE_CHECK_INTERRUPTED",
                last_error_message="检查被服务重启打断，请重新检查",
                updated_at=datetime.now(UTC),
            )
        )
        return int(result.rowcount or 0)

    async def start_datalink_build(self, model: DataSourceModel) -> DataSourceModel:
        """初次建图持久化 building；已有完整图谱的同步重建继续保持 ready。"""

        await self.db.refresh(model)
        if model.status in {DataSourceStatus.DELETING.value, DataSourceStatus.DELETED.value}:
            return model
        if model.schema_cache_json is None:
            return model
        if model.datalink_graph_version is not None and model.status in {
            DataSourceStatus.SCHEMA_READY.value,
            DataSourceStatus.READY.value,
        }:
            model.status = DataSourceStatus.READY.value
            model.last_error_code = None
            model.last_error_message = None
            await self.db.flush()
            return model
        model.status = DataSourceStatus.BUILDING_DATALINK.value
        model.last_error_code = None
        model.last_error_message = None
        await self.db.flush()
        return model

    async def complete_datalink_build(
        self,
        model: DataSourceModel,
        *,
        build_id: str,
        graph_version: str,
        expected_schema_revision: int | None = None,
        expected_connection_revision: int = 0,
    ) -> DataSourceModel:
        """只在当前构建流程仍有效或已被错误投影时发布已确认的可用图谱。"""

        await self.db.refresh(model)
        if model.connection_revision != expected_connection_revision:
            return model
        if (
            expected_schema_revision is not None
            and model.schema_revision != expected_schema_revision
        ):
            return model
        if model.status not in {
            DataSourceStatus.BUILDING_DATALINK.value,
            DataSourceStatus.READY.value,
            DataSourceStatus.FAILED.value,
            DataSourceStatus.SCHEMA_READY.value,
        }:
            return model
        model.status = DataSourceStatus.READY.value
        model.datalink_build_id = build_id
        model.datalink_graph_version = graph_version
        model.last_error_code = None
        model.last_error_message = None
        await self.db.flush()
        return model

    async def fail_datalink_build(
        self,
        model: DataSourceModel,
        *,
        code: str,
        message: str,
    ) -> DataSourceModel:
        """记录失败；保留当前 Schema 或同 revision 的旧完整图谱。"""

        await self.db.refresh(model)
        if model.status not in {
            DataSourceStatus.BUILDING_DATALINK.value,
            DataSourceStatus.READY.value,
        }:
            return model
        model.status = (
            DataSourceStatus.READY.value
            if model.datalink_graph_version is not None
            else DataSourceStatus.SCHEMA_READY.value
        )
        model.last_error_code = code
        model.last_error_message = message
        await self.db.flush()
        return model

    async def note_datalink_unavailable(
        self,
        model: DataSourceModel,
        *,
        message: str,
    ) -> DataSourceModel:
        """网络结果未知时保留当前投影，避免把可能已完成的 Build 伪装成失败。"""

        await self.db.refresh(model)
        if model.status in {
            DataSourceStatus.BUILDING_DATALINK.value,
            DataSourceStatus.READY.value,
        }:
            model.last_error_code = "DATALINK_UNAVAILABLE"
            model.last_error_message = message
            await self.db.flush()
        return model

    async def list_building_datalink(self) -> list[DataSourceModel]:
        """读取服务重启时需要向 DataLink status 对账的有限记录。"""

        result = await self.db.scalars(
            select(DataSourceModel).where(
                DataSourceModel.status == DataSourceStatus.BUILDING_DATALINK.value
            )
        )
        return list(result)

    async def start_deletion(self, model: DataSourceModel) -> DataSourceModel:
        """先写 deleting tombstone，防止图谱清理期间出现新的读取或 Run。"""

        await self.db.refresh(model)
        if model.status == DataSourceStatus.DELETED.value:
            return model
        model.status = DataSourceStatus.DELETING.value
        model.last_error_code = None
        model.last_error_message = None
        await self.db.flush()
        return model

    async def complete_deletion(self, model: DataSourceModel) -> DataSourceModel:
        """图谱清理已确认完成后，才写入不可逆的 deleted tombstone。"""

        await self.db.refresh(model)
        if model.status != DataSourceStatus.DELETING.value:
            return model
        model.status = DataSourceStatus.DELETED.value
        model.last_error_code = None
        model.last_error_message = None
        await self.db.flush()
        return model

    async def fail_deletion(
        self,
        model: DataSourceModel,
        *,
        code: str,
        message: str,
    ) -> DataSourceModel:
        """保留 deleting 状态和可重试错误，避免图谱未清理时伪造删除成功。"""

        await self.db.refresh(model)
        if model.status == DataSourceStatus.DELETING.value:
            model.last_error_code = code
            model.last_error_message = message
            await self.db.flush()
        return model


class SqlAuditRepository:
    """让一次 SQL 尝试始终只使用同一条 Audit 记录。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_proposed(
        self,
        *,
        datasource_id: str,
        schema_revision: int,
        original_sql: str,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        repaired_from_id: str | None = None,
        connection_revision: int = 0,
    ) -> SqlAuditLogModel:
        attempt_no = 0
        if repaired_from_id is not None:
            if run_id is None:
                raise ValueError("SQL 修正来源必须属于一个 Run")
            source = await self.db.get(SqlAuditLogModel, repaired_from_id)
            if (
                source is None
                or source.run_id != run_id
                or source.datasource_id != datasource_id
                or source.schema_revision != schema_revision
                or source.connection_revision != connection_revision
            ):
                raise ValueError("SQL 修正来源与当前 Run 或 Schema 不一致")
            attempt_no = source.attempt_no + 1
        model = SqlAuditLogModel(
            id=make_id("audit"),
            run_id=run_id,
            tool_call_id=tool_call_id,
            datasource_id=datasource_id,
            schema_revision=schema_revision,
            connection_revision=connection_revision,
            attempt_no=attempt_no,
            repaired_from_id=repaired_from_id,
            original_sql=original_sql,
            status="proposed",
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def update(self, model: SqlAuditLogModel, **changes: Any) -> SqlAuditLogModel:
        for key, value in changes.items():
            setattr(model, key, value)
        await self.db.flush()
        return model


class ArtifactRepository:
    """登记 Artifact Metadata；文件本身由 Artifact Store 负责。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        artifact_id: str,
        run_id: str | None,
        session_id: str | None,
        tool_call_id: str | None = None,
        artifact_type: str = "table",
        title: str,
        storage_ref: str | None,
        mime_type: str = "application/json",
        size_bytes: int,
        preview_json: dict[str, Any] | None,
        metadata_json: dict[str, Any],
        content_hash: str,
    ) -> ArtifactModel:
        model = ArtifactModel(
            id=artifact_id,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            type=artifact_type,
            title=title,
            storage_ref=storage_ref,
            mime_type=mime_type,
            size_bytes=size_bytes,
            preview_json=preview_json,
            metadata_json=metadata_json,
            content_hash=content_hash,
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def delete(self, model: ArtifactModel) -> None:
        """撤回尚未交付的 Artifact 索引；文件删除仍由创建用例按顺序负责。"""

        await self.db.delete(model)
        await self.db.flush()

    async def list_for_session(self, session_id: str) -> list[ArtifactModel]:
        """列出 Session 删除前需要逐条清理文件的 Artifact 索引。"""

        run_ids = select(RunModel.id).where(RunModel.session_id == session_id)
        result = await self.db.scalars(
            select(ArtifactModel)
            .where(
                or_(
                    ArtifactModel.session_id == session_id,
                    ArtifactModel.run_id.in_(run_ids),
                )
            )
            .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
        )
        return list(result)


class ArtifactCleanupTaskRepository:
    """登记文件系统暂时无法完成的精确 Artifact 清理任务。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        artifact_id: str | None,
        storage_ref: str,
        error_code: str,
    ) -> ArtifactCleanupTaskModel:
        """仅保存相对 storage_ref；后台重试仍须经过 Artifact Store 的路径校验。"""

        model = ArtifactCleanupTaskModel(
            id=make_id("artifact_cleanup"),
            artifact_id=artifact_id,
            storage_ref=storage_ref,
            last_error_code=error_code,
        )
        self.db.add(model)
        await self.db.flush()
        return model


class DataLinkValidationRepository:
    """唯一负责保存和读取 DataLink 核验任务的 Metadata 仓储。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_idempotency(self, datasource_id: str, idempotency_key: str):
        return await self.db.scalar(
            select(DataLinkValidationModel).where(
                DataLinkValidationModel.datasource_id == datasource_id,
                DataLinkValidationModel.idempotency_key == idempotency_key,
            )
        )

    async def create(
        self,
        datasource_id: str,
        payload: DataLinkValidationRequest,
        *,
        endpoint_fingerprint: str,
        direction: str,
    ):
        model = DataLinkValidationModel(
            id=make_id("datalink_validation"),
            datasource_id=datasource_id,
            relation_id=payload.relation_id,
            graph_version=payload.graph_version,
            schema_revision=payload.schema_revision,
            idempotency_key=payload.idempotency_key,
            endpoint_fingerprint=endpoint_fingerprint,
            direction=direction,
            audit_log_ids_json=[],
            artifact_ids_json=[],
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def get(self, validation_id: str):
        return await self.db.get(DataLinkValidationModel, validation_id)

    async def list_for_datasource(self, datasource_id: str):
        result = await self.db.scalars(
            select(DataLinkValidationModel)
            .where(DataLinkValidationModel.datasource_id == datasource_id)
            .order_by(DataLinkValidationModel.created_at.desc(), DataLinkValidationModel.id.desc())
        )
        return list(result)

    async def recover_interrupted(self) -> int:
        result = await self.db.scalars(
            select(DataLinkValidationModel).where(DataLinkValidationModel.status == "running")
        )
        finished_at = datetime.now(UTC)
        count = 0
        for model in result:
            model.status = "interrupted"
            model.error_code = "PROCESS_RESTARTED"
            model.finished_at = finished_at
            count += 1
        if count:
            await self.db.flush()
        return count

    async def request_cancel(self, model: DataLinkValidationModel) -> DataLinkValidationModel:
        model.cancel_requested = True
        await self.db.flush()
        return model

    async def finish(
        self,
        model: DataLinkValidationModel,
        *,
        status: str,
        metrics: dict[str, Any] | None = None,
        audit_log_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        error_code: str | None = None,
    ) -> DataLinkValidationModel:
        if model.status != "running":
            return model
        if model.cancel_requested and status in {"completed", "partial"}:
            status = "canceled"
            error_code = error_code or "QUERY_CANCELED"
        model.status = status
        if metrics is not None:
            model.metrics_json = metrics
        if audit_log_ids is not None:
            model.audit_log_ids_json = audit_log_ids
        if artifact_ids is not None:
            model.artifact_ids_json = artifact_ids
        model.error_code = error_code
        model.finished_at = datetime.now(UTC)
        await self.db.flush()
        return model


class RunDatalinkConsumptionRepository:
    """Persist and read Run-scoped DataLink consumption records."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, payload: DataLinkConsumptionCreate):
        next_seq = int(
            await self.db.scalar(
                select(func.coalesce(func.max(RunDatalinkConsumptionModel.seq), 0)).where(
                    RunDatalinkConsumptionModel.run_id == payload.run_id
                )
            )
            or 0
        )
        model = RunDatalinkConsumptionModel(
            id=make_id("datalink_consumption"),
            run_id=payload.run_id,
            stage=payload.stage,
            seq=next_seq + 1,
            query=payload.query,
            focus=payload.focus,
            max_nodes=payload.max_nodes,
            schema_revision=payload.schema_revision,
            graph_version=payload.graph_version,
            mode=payload.mode,
            payload_status=payload.payload_status,
            returned_status=payload.returned_status,
            consumer_receipt_status=payload.consumer_receipt_status,
            is_truncated=payload.is_truncated,
            tool_call_id=payload.tool_call_id,
            payload_version=payload.payload_version,
            payload_json=(
                payload.semantic_context.model_dump(mode="json")
                if payload.semantic_context is not None
                else None
            ),
            summary_json=payload.summary.model_dump(mode="json"),
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def list_for_run(self, run_id: str):
        result = await self.db.scalars(
            select(RunDatalinkConsumptionModel)
            .where(RunDatalinkConsumptionModel.run_id == run_id)
            .order_by(RunDatalinkConsumptionModel.seq.asc(), RunDatalinkConsumptionModel.id.asc())
        )
        return list(result)

    async def get(self, consumption_id: str):
        return await self.db.get(RunDatalinkConsumptionModel, consumption_id)


class SecretRepository:
    """管理加密后的模型密钥，调用方不能通过它取得明文以外的业务信息。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, encrypted_value: str) -> SecretModel:
        model = SecretModel(id=make_id("secret"), encrypted_value=encrypted_value)
        self.db.add(model)
        await self.db.flush()
        return model

    async def get(self, secret_id: str) -> SecretModel | None:
        return await self.db.get(SecretModel, secret_id)

    async def update(self, model: SecretModel, encrypted_value: str) -> SecretModel:
        model.encrypted_value = encrypted_value
        await self.db.flush()
        return model

    async def delete_by_id(self, secret_id: str | None) -> None:
        if secret_id is None:
            return
        await self.db.execute(delete(SecretModel).where(SecretModel.id == secret_id))
        await self.db.flush()


class ModelProfileRepository:
    """管理模型配置及本地单用户场景下的唯一激活项。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, payload: ModelProfileCreate, secret_ref: str) -> ModelProfileModel:
        model = ModelProfileModel(
            id=make_id("profile"),
            name=payload.name,
            provider=payload.provider,
            model_name=payload.model_name,
            base_url=str(payload.base_url),
            temperature=payload.temperature,
            run_timeout_seconds=payload.run_timeout_seconds,
            context_window_tokens=payload.context_window_tokens,
            secret_ref=secret_ref,
            status=ModelProfileStatus.CREATED.value,
            is_active=False,
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def list(self, *, offset: int, limit: int) -> tuple[list[ModelProfileModel], int]:
        total = await self.db.scalar(select(func.count()).select_from(ModelProfileModel))
        result = await self.db.scalars(
            select(ModelProfileModel)
            .order_by(ModelProfileModel.updated_at.desc(), ModelProfileModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), int(total or 0)

    async def get(self, profile_id: str) -> ModelProfileModel | None:
        return await self.db.get(ModelProfileModel, profile_id)

    async def get_active(self) -> ModelProfileModel | None:
        """读取本地单人 Demo 唯一激活模型，数据库部分唯一索引保证至多一条。"""

        result = await self.db.scalars(
            select(ModelProfileModel).where(ModelProfileModel.is_active.is_(True))
        )
        return result.one_or_none()

    async def update(
        self, model: ModelProfileModel, payload: ModelProfileUpdate
    ) -> ModelProfileModel:
        changes = payload.model_dump(exclude_unset=True, exclude={"api_key"})
        capability_affecting_fields = {
            "provider",
            "model_name",
            "base_url",
            "temperature",
            "run_timeout_seconds",
            "context_window_tokens",
        }
        capability_cache_invalid = payload.api_key is not None
        for key, value in changes.items():
            normalized_value = str(value) if key == "base_url" and value is not None else value
            if key in capability_affecting_fields and getattr(model, key) != normalized_value:
                capability_cache_invalid = True
            setattr(model, key, normalized_value)
        if capability_cache_invalid:
            self._clear_capabilities(model)
        await self.db.flush()
        return model

    async def record_capabilities(
        self,
        model: ModelProfileModel,
        capabilities: ModelProfileCapabilities,
    ) -> ModelProfileModel:
        """保存一次真实探测结果；未通过 Tool Calling 的 Profile 不能用于分析 Run。"""

        model.tool_calling_supported = capabilities.tool_calling_supported
        model.final_output_mode = capabilities.final_output_mode
        model.capability_contract_version = capabilities.capability_contract_version
        model.capability_fingerprint = capabilities.capability_fingerprint
        model.capability_checked_at = datetime.now(UTC)
        model.status = (
            ModelProfileStatus.TESTED.value
            if capabilities.tool_calling_supported
            else ModelProfileStatus.FAILED.value
        )
        await self.db.flush()
        return model

    async def set_status(
        self, model: ModelProfileModel, status: ModelProfileStatus
    ) -> ModelProfileModel:
        model.status = status.value
        await self.db.flush()
        return model

    async def activate(self, model: ModelProfileModel) -> ModelProfileModel:
        """激活前清空其它 Profile，保证本地单用户场景只有一个主模型。"""

        await self.db.execute(update(ModelProfileModel).values(is_active=False))
        model.is_active = True
        await self.db.flush()
        return model

    async def delete(self, model: ModelProfileModel) -> None:
        await self.db.delete(model)
        await self.db.flush()

    @staticmethod
    def _clear_capabilities(model: ModelProfileModel) -> None:
        """任何会改变模型请求的字段都必须废弃此前的探测结果。"""

        model.tool_calling_supported = None
        model.final_output_mode = None
        model.capability_contract_version = None
        model.capability_fingerprint = None
        model.capability_checked_at = None
        model.status = ModelProfileStatus.CREATED.value


class RunRepository:
    """Run 的持久化读写；状态与事件仍由阶段五运行时的专属所有者控制。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, run_id: str) -> RunModel | None:
        return await self.db.get(RunModel, run_id)

    async def mark_historical_summary_projection(
        self, run_id: str, *, status: str, attempted_at: datetime | None = None
    ) -> RunModel | None:
        """记录摘要投影终态；仅 failed 允许下一次 reconcile 重试。"""

        model = await self.get(run_id)
        if model is None:
            return None
        model.historical_summary_projection_status = status
        model.historical_summary_projection_attempts = (
            model.historical_summary_projection_attempts or 0
        ) + 1
        model.historical_summary_projection_last_attempt_at = attempted_at or datetime.now(UTC)
        await self.db.flush()
        return model

    async def create(
        self,
        *,
        run_id: str | None = None,
        session_id: str,
        datasource_id: str,
        user_message_id: str | None,
        question: str,
        idempotency_key: str,
        model_profile_id: str,
        model_provider: str,
        model_name: str,
        schema_revision: int,
        datalink_graph_version: str | None,
        run_timeout_seconds: int,
        final_output_mode: FinalOutputMode,
        model_capability_fingerprint: str,
        input_snapshot_ref: str | None,
        connection_revision: int = 0,
        mask_fields: list[str] | None = None,
    ) -> RunModel:
        """创建已解析身份的 queued Run，绝不写入密钥或完整模型配置。"""

        model = RunModel(
            id=run_id or make_id("run"),
            session_id=session_id,
            datasource_id=datasource_id,
            user_message_id=user_message_id,
            question=question,
            idempotency_key=idempotency_key,
            status=RunStatus.QUEUED.value,
            model_profile_id=model_profile_id,
            model_provider=model_provider,
            model_name=model_name,
            schema_revision=schema_revision,
            connection_revision=connection_revision,
            datalink_graph_version=datalink_graph_version,
            input_snapshot_ref=input_snapshot_ref,
            mask_fields_json=sorted(set(mask_fields or [])),
            run_timeout_seconds=run_timeout_seconds,
            final_output_mode=final_output_mode,
            model_capability_fingerprint=model_capability_fingerprint,
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def select_protocol_if_unset(
        self, run_id: str, protocol_id: RunProtocolId
    ) -> Literal["selected", "idempotent", "conflict", "missing"]:
        """以数据库条件更新保证一个 Run 的协议只能选择一次。"""

        result = await self.db.execute(
            update(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.protocol_id.is_(None),
                RunModel.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
            )
            .values(protocol_id=protocol_id)
        )
        if result.rowcount == 1:
            await self.db.flush()
            return "selected"

        model = await self.get(run_id)
        if model is None or model.status not in {
            RunStatus.QUEUED.value,
            RunStatus.RUNNING.value,
        }:
            return "missing"
        return "idempotent" if model.protocol_id == protocol_id else "conflict"

    async def get_by_session_and_idempotency_key(
        self, session_id: str, idempotency_key: str
    ) -> RunModel | None:
        result = await self.db.scalars(
            select(RunModel).where(
                RunModel.session_id == session_id,
                RunModel.idempotency_key == idempotency_key,
            )
        )
        return result.one_or_none()

    async def get_active_for_session(self, session_id: str) -> RunModel | None:
        """返回会话唯一可执行 Run；部分唯一索引是并发情形的最终保护。"""

        result = await self.db.scalars(
            select(RunModel)
            .where(
                RunModel.session_id == session_id,
                RunModel.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
            )
            .order_by(RunModel.created_at.asc())
        )
        return result.first()

    async def list_for_session(
        self, session_id: str, *, offset: int, limit: int, q: str | None = None
    ) -> tuple[list[RunModel], int]:
        filters = [RunModel.session_id == session_id]
        if q:
            filters.append(_like_contains(RunModel.question, q))
        total = await self.db.scalar(select(func.count()).select_from(RunModel).where(*filters))
        result = await self.db.scalars(
            select(RunModel)
            .where(*filters)
            .order_by(RunModel.created_at.desc(), RunModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), int(total or 0)

    async def list_active_for_datasource(self, datasource_id: str) -> list[RunModel]:
        result = await self.db.scalars(
            select(RunModel).where(
                RunModel.datasource_id == datasource_id,
                RunModel.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
            )
        )
        return list(result)

    async def list_interrupted(self) -> list[RunModel]:
        """服务启动恢复器只读取遗留活跃 Run，不根据旧字段恢复执行。"""

        result = await self.db.scalars(
            select(RunModel).where(
                RunModel.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value))
            )
        )
        return list(result)


class MessageRepository:
    """会话消息的顺序持久化；正式 Assistant Message 由 RunFinalizer 调用。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, message_id: str) -> MessageModel | None:
        """按主键读取单条消息，避免上层记忆服务直接访问 ORM 会话。"""

        return await self.db.get(MessageModel, message_id)

    async def create(
        self,
        *,
        session_id: str,
        role: MessageRole,
        content_text: str,
        datasource_id: str | None = None,
        run_id: str | None = None,
        answer_evidence_refs: list[str] | None = None,
        position: int | None = None,
    ) -> MessageModel:
        next_position = position
        if next_position is None:
            next_position = (
                int(
                    await self.db.scalar(
                        select(func.coalesce(func.max(MessageModel.position), 0)).where(
                            MessageModel.session_id == session_id
                        )
                    )
                    or 0
                )
                + 1
            )
        model = MessageModel(
            id=make_id("message"),
            session_id=session_id,
            datasource_id=datasource_id,
            run_id=run_id,
            role=role.value,
            content_text=content_text,
            answer_evidence_refs_json=(
                list(dict.fromkeys(answer_evidence_refs))
                if answer_evidence_refs is not None
                else None
            ),
            position=next_position,
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def list_recent(
        self,
        session_id: str,
        *,
        limit: int,
        datasource_id: str | None = None,
        before_position: int | None = None,
    ) -> list[MessageModel]:
        filters = [MessageModel.session_id == session_id]
        if datasource_id is not None:
            filters.append(MessageModel.datasource_id == datasource_id)
        if before_position is not None:
            filters.append(MessageModel.position <= before_position)
        result = await self.db.scalars(
            select(MessageModel).where(*filters).order_by(MessageModel.position.desc()).limit(limit)
        )
        return list(reversed(list(result)))

    async def count_for_session(self, session_id: str) -> int:
        total = await self.db.scalar(
            select(func.count())
            .select_from(MessageModel)
            .where(MessageModel.session_id == session_id)
        )
        return int(total or 0)

    async def max_position(self, session_id: str) -> int:
        """读取 Session 级消息序列 high-water。"""
        value = await self.db.scalar(
            select(func.coalesce(func.max(MessageModel.position), 0)).where(
                MessageModel.session_id == session_id
            )
        )
        return int(value or 0)

    async def list_after_position(
        self,
        session_id: str,
        *,
        after_position: int,
        limit: int,
    ) -> list[MessageModel]:
        """按位置读取摘要覆盖范围之后的消息，供会话记忆保留近期原文。"""

        result = await self.db.scalars(
            select(MessageModel)
            .where(
                MessageModel.session_id == session_id,
                MessageModel.position > after_position,
            )
            .order_by(MessageModel.position.asc())
            .limit(limit)
        )
        return list(result)

    async def list_between_positions(
        self,
        session_id: str,
        *,
        start_exclusive: int,
        end_inclusive: int,
        datasource_id: str | None = None,
    ) -> list[MessageModel]:
        """读取摘要覆盖区间内的有序消息，不让记忆服务直接查询 ORM。"""

        filters = [
            MessageModel.session_id == session_id,
            MessageModel.position > start_exclusive,
            MessageModel.position <= end_inclusive,
        ]
        if datasource_id is not None:
            filters.append(MessageModel.datasource_id == datasource_id)
        result = await self.db.scalars(
            select(MessageModel).where(*filters).order_by(MessageModel.position.asc())
        )
        return list(result)

    async def list_for_session(
        self,
        session_id: str,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[MessageModel], int]:
        """按对话顺序分页读取已保存消息，不触发任何 Run 或模型调用。"""

        total = await self.db.scalar(
            select(func.count())
            .select_from(MessageModel)
            .where(MessageModel.session_id == session_id)
        )
        result = await self.db.scalars(
            select(MessageModel)
            .where(MessageModel.session_id == session_id)
            .order_by(MessageModel.position.asc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), int(total or 0)


class RunHistoryRepository:
    """只读查询 Run 的已保存投影，绝不重建运行时或外部连接。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_events(self, run_id: str) -> list[RunEventModel]:
        result = await self.db.scalars(
            select(RunEventModel)
            .where(RunEventModel.run_id == run_id)
            .order_by(RunEventModel.seq.asc())
        )
        return list(result)

    async def list_tool_calls(self, run_id: str) -> list[ToolCallModel]:
        result = await self.db.scalars(
            select(ToolCallModel)
            .where(ToolCallModel.run_id == run_id)
            .order_by(ToolCallModel.started_at.asc(), ToolCallModel.id.asc())
        )
        return list(result)

    async def list_sql_audits(self, run_id: str) -> list[SqlAuditLogModel]:
        result = await self.db.scalars(
            select(SqlAuditLogModel)
            .where(SqlAuditLogModel.run_id == run_id)
            .order_by(SqlAuditLogModel.created_at.asc(), SqlAuditLogModel.id.asc())
        )
        return list(result)

    async def list_artifacts(self, run_id: str) -> list[ArtifactModel]:
        result = await self.db.scalars(
            select(ArtifactModel)
            .where(ArtifactModel.run_id == run_id)
            .order_by(ArtifactModel.created_at.asc(), ArtifactModel.id.asc())
        )
        return list(result)

    async def get_artifact(self, artifact_id: str) -> ArtifactModel | None:
        return await self.db.get(ArtifactModel, artifact_id)


class SessionPreferencesRepository:
    """Session 偏好的唯一持久化入口。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, session_id: str) -> SessionPreferencesModel | None:
        return await self.db.get(SessionPreferencesModel, session_id)

    async def upsert(
        self,
        *,
        session_id: str,
        through_message_position: int,
        preferences: dict[str, Any],
    ) -> SessionPreferencesModel:
        model = await self.get(session_id)
        if model is None:
            model = SessionPreferencesModel(
                session_id=session_id,
                revision=1,
                through_message_position=through_message_position,
                preferences_json=dict(preferences),
            )
            self.db.add(model)
        else:
            model.revision += 1
            model.through_message_position = max(
                model.through_message_position, through_message_position
            )
            model.preferences_json = dict(preferences)
        await self.db.flush()
        return model


class DatasourceConversationStateRepository:
    """按 Session + DataSource 隔离 pending clarification 的唯一入口。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(
        self, session_id: str, datasource_id: str
    ) -> DatasourceConversationStateModel | None:
        return await self.db.get(
            DatasourceConversationStateModel,
            (session_id, datasource_id),
        )

    async def set_pending(
        self,
        *,
        session_id: str,
        datasource_id: str,
        pending_id: str,
        pending_json: dict[str, Any],
        expected_revision: int | None = None,
    ) -> DatasourceConversationStateModel | None:
        model = await self.get(session_id, datasource_id)
        current_revision = model.revision if model is not None else 0
        if expected_revision is not None and current_revision != expected_revision:
            return None
        if model is None:
            model = DatasourceConversationStateModel(
                session_id=session_id,
                datasource_id=datasource_id,
                revision=1,
                pending_id=pending_id,
                pending_json=dict(pending_json),
            )
            self.db.add(model)
        else:
            model.revision += 1
            model.pending_id = pending_id
            model.pending_json = dict(pending_json)
        await self.db.flush()
        return model

    async def clear(
        self,
        *,
        session_id: str,
        datasource_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        model = await self.get(session_id, datasource_id)
        if model is None:
            return True
        if expected_revision is not None and model.revision != expected_revision:
            return False
        model.revision += 1
        model.pending_id = None
        model.pending_json = None
        await self.db.flush()
        return True


class HistoricalAnswerSummaryRepository:
    """历史答案摘要按 source_run_id 幂等追加，绝不覆盖其它 Run。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_recent(
        self, *, session_id: str, datasource_id: str, limit: int = 3
    ) -> list[HistoricalAnswerSummaryModel]:
        result = await self.db.scalars(
            select(HistoricalAnswerSummaryModel)
            .where(
                HistoricalAnswerSummaryModel.session_id == session_id,
                HistoricalAnswerSummaryModel.datasource_id == datasource_id,
            )
            .order_by(
                HistoricalAnswerSummaryModel.source_run_finished_at.desc(),
                HistoricalAnswerSummaryModel.source_run_id.desc(),
            )
            .limit(limit)
        )
        return list(result)

    async def list_by_source_run(self, source_run_id: str) -> HistoricalAnswerSummaryModel | None:
        return await self.db.scalar(
            select(HistoricalAnswerSummaryModel).where(
                HistoricalAnswerSummaryModel.source_run_id == source_run_id
            )
        )

    async def exists_for_session(self, session_id: str) -> bool:
        """历史摘要是不可变上下文事实，拥有者删除前必须显式保留。"""

        result = await self.db.scalar(
            select(HistoricalAnswerSummaryModel.id)
            .where(HistoricalAnswerSummaryModel.session_id == session_id)
            .limit(1)
        )
        return result is not None

    async def create_if_absent(
        self,
        *,
        session_id: str,
        datasource_id: str,
        source_run_id: str,
        topic: str,
        content_text: str,
        data_freshness: str,
        source_run_finished_at: datetime | None = None,
    ) -> HistoricalAnswerSummaryModel:
        source_run = await self.db.get(RunModel, source_run_id)
        if (
            source_run is None
            or source_run.session_id != session_id
            or source_run.datasource_id != datasource_id
        ):
            raise ValueError("historical summary source Run ownership mismatch")
        existing = await self.db.scalar(
            select(HistoricalAnswerSummaryModel).where(
                HistoricalAnswerSummaryModel.source_run_id == source_run_id
            )
        )
        if existing is not None:
            return existing
        model = HistoricalAnswerSummaryModel(
            id=make_id("history"),
            session_id=session_id,
            datasource_id=datasource_id,
            source_run_id=source_run_id,
            source_run_finished_at=source_run_finished_at or datetime.now(UTC),
            topic=topic[:300],
            content_text=content_text[:1_200],
            provenance="historical_answer_summary",
            data_freshness=data_freshness[:40],
        )
        self.db.add(model)
        await self.db.flush()
        return model


def _schema_structure(schema: dict[str, Any]) -> dict[str, Any]:
    """去掉行数等易变信息，只比较会决定 Schema revision 的结构。"""

    return {
        "datasource_id": schema.get("datasource_id"),
        "dialect": schema.get("dialect"),
        "tables": [
            {
                "name": table.get("name"),
                "columns": table.get("columns", []),
                "primary_key": table.get("primary_key", []),
                "foreign_keys": table.get("foreign_keys", []),
            }
            for table in schema.get("tables", [])
        ],
    }
