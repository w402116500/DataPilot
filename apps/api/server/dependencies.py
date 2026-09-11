from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated

from application.agent_ports import DataLinkMcpPort
from application.artifacts import ArtifactStore
from application.datalink_client import DataLinkManagementPort
from application.datalink_preview import DataLinkPreviewService
from application.datalink_validation import DataLinkValidationService
from application.datalink_versions import DataLinkVersionsService
from application.datasource_factory import build_datasource_service
from application.datasources import DataSourceService
from application.model_capability_probe import LangChainModelCapabilityProbe
from application.model_profiles import ModelProfileService
from application.script_workspace import SessionWorkspaceManager
from application.secret_cipher import SecretCipher
from application.sessions import SessionService
from fastapi import Depends, Request
from metadata.repositories import (
    ArtifactRepository,
    DataLinkValidationRepository,
    DataSourceRepository,
    ModelProfileRepository,
    SecretRepository,
    SessionRepository,
)
from runtime.run_cancel_registry import RunCancellation
from runtime.run_context_resolver import RunContextResolver
from runtime.run_event_pipeline import RunEventPipeline
from runtime.run_event_stream import RunEventStream
from runtime.run_history_service import RunHistoryService
from runtime.run_service import RunService
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_cipher(settings: Annotated[Settings, Depends(get_settings)]) -> SecretCipher:
    return SecretCipher(settings.secret_master_key)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """为单个 HTTP 请求提供事务；正常提交，异常时统一回滚。"""

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_session_service(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionService:
    return SessionService(
        SessionRepository(db),
        DataSourceRepository(db),
        artifact_store=ArtifactStore(
            ArtifactRepository(db), request.app.state.settings.artifact_root
        ),
        session_workspace=SessionWorkspaceManager(
            request.app.state.settings.session_workspace_root
        ),
        run_lifecycle=request.app.state.run_lifecycle,
    )


def get_run_service(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    cipher: Annotated[SecretCipher, Depends(get_cipher)],
) -> RunService:
    """装配请求阶段的 Run 创建链，后台执行器只接收已提交后的内存上下文。"""

    return RunService(
        db=db,
        resolver=RunContextResolver(db, settings, cipher),
        events=RunEventPipeline(db, notify=request.app.state.run_event_notifier.notify),
        schedule=request.app.state.run_executor.schedule,
        request_cancel=request.app.state.run_executor.cancel,
    )


def get_run_event_stream(request: Request) -> RunEventStream:
    """SSE 使用短生命周期数据库会话，不能借用 HTTP 请求事务。"""

    return RunEventStream(
        session_factory=request.app.state.session_factory,
        notifier=request.app.state.run_event_notifier,
    )


def get_run_history_service(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RunHistoryService:
    """装配仅使用当前请求 Metadata 会话的历史读取服务。"""

    return RunHistoryService(
        db,
        artifact_store=ArtifactStore(
            ArtifactRepository(db), request.app.state.settings.artifact_root
        ),
    )


def get_model_profile_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    cipher: Annotated[SecretCipher, Depends(get_cipher)],
) -> ModelProfileService:
    return ModelProfileService(
        ModelProfileRepository(db),
        SecretRepository(db),
        cipher,
        LangChainModelCapabilityProbe(),
    )


def get_datasource_service(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DataSourceService:
    """通过 FastAPI 依赖注入，为路由提供完整的数据源服务链。"""

    return build_datasource_service(
        db,
        settings,
        datalink_client=request.app.state.datalink_client,
        run_lifecycle=request.app.state.run_lifecycle,
    )


def get_datalink_versions_service(
    datasources: Annotated[DataSourceService, Depends(get_datasource_service)],
) -> DataLinkVersionsService:
    return DataLinkVersionsService(datasources, datasources.datalink_client)


def get_datalink_validation_service(
    request: Request,
    datasources: Annotated[DataSourceService, Depends(get_datasource_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DataLinkValidationService:
    return DataLinkValidationService(
        datasources,
        DataLinkValidationRepository(db),
        db=db,
        cancels=request.app.state.validation_cancels,
        query_timeout_seconds=settings.query_timeout_seconds,
    )


def get_datalink_preview_service(
    datasources: Annotated[DataSourceService, Depends(get_datasource_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DataLinkPreviewService:
    return DataLinkPreviewService(
        datasources,
        DataLinkMcpPort(
            endpoint=settings.datalink_mcp_url,
            timeout_seconds=settings.datalink_timeout_seconds,
        ),
    )


async def get_request_cancellation(request: Request) -> AsyncIterator[RunCancellation]:
    """Cancel external preview work when its HTTP consumer disconnects."""

    cancellation = RunCancellation()

    async def watch_disconnect() -> None:
        while not await request.is_disconnected():
            await asyncio.sleep(0.1)
        cancellation.cancel()

    watcher = asyncio.create_task(watch_disconnect())
    try:
        yield cancellation
    finally:
        cancellation.cancel()
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


async def inspect_datasource_in_background(
    session_factory,
    settings: Settings,
    datasource_id: str,
    datalink_client: DataLinkManagementPort,
) -> None:
    """后台检查使用独立事务，避免借用已返回 HTTP 请求的数据库会话。"""

    async with session_factory() as db:
        service = build_datasource_service(db, settings, datalink_client=datalink_client)
        await service.inspect(datasource_id)
        await db.commit()
