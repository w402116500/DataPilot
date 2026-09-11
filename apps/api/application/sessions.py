from __future__ import annotations

from typing import TYPE_CHECKING

from contracts.api import Page, PageResult
from contracts.errors import AppError, ErrorCode
from contracts.sessions import MessageRead, SessionCreate, SessionRead, SessionUpdate
from contracts.status import DataSourceStatus
from metadata.models import MessageModel, SessionModel
from metadata.repositories import (
    ArtifactCleanupTaskRepository,
    ArtifactRepository,
    DataSourceRepository,
    MessageRepository,
    RunRepository,
    SessionRepository,
)

from application.artifacts import ArtifactStore
from application.script_workspace import SessionWorkspaceManager

if TYPE_CHECKING:
    from runtime.run_lifecycle import RunLifecycleCoordinator


def to_session_read(model: SessionModel) -> SessionRead:
    """把内部 Session ORM 模型转换成对外 API 契约。"""

    return SessionRead(
        id=model.id,
        title=model.title,
        selected_datasource_id=model.selected_datasource_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
        last_message_at=model.last_message_at,
    )


def _normalized_q(q: str | None) -> str | None:
    if q is None:
        return None
    cleaned = q.strip()
    return cleaned or None


def to_message_read(message: MessageModel) -> MessageRead:
    return MessageRead(
        id=message.id,
        session_id=message.session_id,
        run_id=message.run_id,
        role=message.role,
        content_text=message.content_text,
        answer_evidence_refs=message.answer_evidence_refs_json,
        position=message.position,
        created_at=message.created_at,
    )


class SessionService:
    """管理会话与当前数据源选择，不直接执行数据查询或 Run。"""

    def __init__(
        self,
        repository: SessionRepository,
        datasource: DataSourceRepository,
        *,
        artifact_store: ArtifactStore,
        session_workspace: SessionWorkspaceManager | None = None,
        run_lifecycle: RunLifecycleCoordinator | None = None,
    ) -> None:
        self.repository = repository
        self.datasource = datasource
        self.artifact_store = artifact_store
        self.session_workspace = session_workspace
        self.run_lifecycle = run_lifecycle

    async def create(self, payload: SessionCreate) -> SessionRead:
        """创建会话；若指定数据源，必须已经完整准备好供 Run 使用。"""

        await self._validate_datasource_binding(payload.selected_datasource_id)
        model = await self.repository.create(payload)
        return to_session_read(model)

    async def list(self, page: Page, *, q: str | None = None) -> PageResult[SessionRead]:
        """分页返回会话摘要，排序与持久层保持一致。"""

        items, total = await self.repository.list(
            offset=page.offset,
            limit=page.page_size,
            q=_normalized_q(q),
        )
        return PageResult(
            items=[to_session_read(item) for item in items],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def get(self, session_id: str) -> SessionRead:
        """读取单个会话，不存在时返回稳定的业务错误。"""

        model = await self._get_model(session_id)
        return to_session_read(model)

    async def update(self, session_id: str, payload: SessionUpdate) -> SessionRead:
        """更新标题或当前数据源；活跃 Run 存在时不能切换数据源。"""

        model = await self._get_model(session_id)
        if "selected_datasource_id" in payload.model_fields_set:
            active = await RunRepository(self.repository.db).get_active_for_session(session_id)
            if active is not None:
                raise AppError(
                    ErrorCode.RUN_ALREADY_ACTIVE,
                    "当前会话正在分析，不能切换数据源",
                    status_code=409,
                    details={"run_id": active.id},
                )
            await self._validate_datasource_binding(payload.selected_datasource_id)
        model = await self.repository.update(model, payload)
        return to_session_read(model)

    async def delete(self, session_id: str) -> None:
        """先收尾活跃 Run，再清理 Artifact 文件与工作区；摘要随 Session 级联删除。"""

        model = await self._get_model(session_id)
        if self.run_lifecycle is not None:
            await self.run_lifecycle.cancel_for_session(session_id, reason="session_deleted")
        artifacts = await ArtifactRepository(self.repository.db).list_for_session(session_id)
        cleanup_tasks = ArtifactCleanupTaskRepository(self.repository.db)
        cleaned_refs: set[str] = set()
        for artifact in artifacts:
            storage_ref = artifact.storage_ref
            if storage_ref is None or storage_ref in cleaned_refs:
                continue
            cleaned_refs.add(storage_ref)
            try:
                await self.artifact_store.delete_registered_file(storage_ref)
            except (OSError, ValueError):
                # Metadata 随后会级联删除；任务仅保留精确相对引用供后续重试。
                await cleanup_tasks.create(
                    artifact_id=artifact.id,
                    storage_ref=storage_ref,
                    error_code="ARTIFACT_DELETE_FAILED",
                )
        await self.repository.delete(model)
        if self.session_workspace is not None:
            await self.session_workspace.delete_session(session_id)

    async def list_messages(
        self,
        session_id: str,
        page: Page,
        *,
        before_position: int | None = None,
    ) -> PageResult[MessageRead]:
        """返回最近一窗已持久化消息；向上翻时用 before_position，不改 list_recent。"""

        await self._get_model(session_id)
        messages = MessageRepository(self.repository.db)
        total = await messages.count_for_session(session_id)
        repo_before = None if before_position is None else before_position - 1
        if repo_before is not None and repo_before < 1:
            items: list[MessageModel] = []
        else:
            items = await messages.list_recent(
                session_id,
                limit=page.page_size,
                before_position=repo_before,
            )
        return PageResult(
            items=[to_message_read(message) for message in items],
            total=total,
            page=1,
            page_size=page.page_size,
        )

    async def _get_model(self, session_id: str) -> SessionModel:
        """查找会话 ORM 模型，统一处理不存在的情况。"""

        model = await self.repository.get(session_id)
        if model is None:
            raise AppError(
                ErrorCode.SESSION_NOT_FOUND,
                "Session does not exist",
                status_code=404,
                details={"session_id": session_id},
            )
        return model

    async def _validate_datasource_binding(self, datasource_id: str | None) -> None:
        """会话只保存已 ready 的数据源，避免后续创建 Run 时才暴露无效选择。"""

        if datasource_id is None:
            return
        datasource = await self.datasource.get(datasource_id)
        if datasource is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_FOUND,
                "数据源不存在",
                status_code=404,
                details={"datasource_id": datasource_id},
            )
        if (
            datasource.status
            not in {
                DataSourceStatus.SCHEMA_READY.value,
                DataSourceStatus.READY.value,
            }
            or not datasource.mask_fields_confirmed
        ):
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未准备完成或未确认遮蔽字段，不能绑定到会话",
                status_code=409,
                details={"datasource_id": datasource_id},
            )
