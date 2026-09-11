from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from application.datalink_client import DataLinkClient, DataLinkClientError, DataLinkManagementPort
from application.datasources import DataSourceService
from application.run_runtime import RunRuntimeService
from contracts.errors import AppError
from contracts.health import HealthResponse
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.repositories import DataLinkValidationRepository, DataSourceRepository
from runtime.run_cancel_registry import RunCancelRegistry
from runtime.run_event_pipeline import RunEventNotifier
from runtime.run_executor import RunExecutor
from runtime.run_lifecycle import RunLifecycleCoordinator, RunRecoveryService
from runtime.validation_cancel_registry import ValidationCancelRegistry
from sqlalchemy import text

from server.config import Settings
from server.dependencies import build_datasource_service
from server.error_handlers import (
    handle_app_error,
    handle_unexpected_error,
    handle_validation_error,
)
from server.middleware import RequestIdMiddleware
from server.routes.datasources import router as datasources_router
from server.routes.model_profiles import router as model_profiles_router
from server.routes.runs import router as runs_router
from server.routes.sessions import router as sessions_router


def create_app(
    settings: Settings | None = None,
    *,
    datalink_client: DataLinkManagementPort | None = None,
) -> FastAPI:
    """创建主后端，并在启动时恢复被服务重启打断的数据源检查。"""

    app_settings = settings or Settings()
    app_settings.apply_langsmith_environment()
    app_settings.ensure_storage_roots()
    engine = create_sqlite_engine(app_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    resolved_datalink_client = datalink_client or DataLinkClient(
        app_settings.datalink_base_url,
        timeout_seconds=app_settings.datalink_timeout_seconds,
    )
    run_event_notifier = RunEventNotifier()
    validation_cancels = ValidationCancelRegistry()

    def build_runtime(db, events) -> RunRuntimeService:
        return RunRuntimeService(
            db=db,
            settings=app_settings,
            datalink_management_client=resolved_datalink_client,
            events=events,
        )

    run_executor = RunExecutor(
        session_factory=session_factory,
        notifier=run_event_notifier,
        registry=RunCancelRegistry(),
        runtime_factory=build_runtime,
    )
    run_lifecycle = RunLifecycleCoordinator(
        session_factory=session_factory,
        notifier=run_event_notifier,
        executor=run_executor,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        app.state.db_engine = engine
        app.state.session_factory = session_factory
        app.state.run_event_notifier = run_event_notifier
        app.state.run_executor = run_executor
        app.state.run_lifecycle = run_lifecycle
        try:
            await RunRecoveryService(
                session_factory=session_factory,
                notifier=run_event_notifier,
            ).recover()
            async with session_factory() as session:
                await DataSourceRepository(session).recover_interrupted_inspections()
                await DataLinkValidationRepository(session).recover_interrupted()
                datasource_service: DataSourceService = build_datasource_service(
                    session,
                    app_settings,
                    datalink_client=resolved_datalink_client,
                )
                await datasource_service.recover_interrupted_datalink_builds()
                await session.commit()
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="DataPilot API", version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.db_engine = engine
    app.state.session_factory = session_factory
    app.state.datalink_client = resolved_datalink_client
    app.state.validation_cancels = validation_cancels
    app.state.run_event_notifier = run_event_notifier
    app.state.run_executor = run_executor
    app.state.run_lifecycle = run_lifecycle

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """确认主后端基础依赖，并安全投影独立 DataLink 的可用性。"""

        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        try:
            datalink_health = await request.app.state.datalink_client.health()
        except DataLinkClientError:
            datalink = "unavailable"
            status = "degraded"
        else:
            datalink = "ok" if datalink_health.status == "healthy" else "degraded"
            status = "healthy" if datalink_health.status == "healthy" else "degraded"
        return HealthResponse(
            status=status,
            metadata="ok",
            datasource_root="ok",
            artifact_root="ok",
            datalink=datalink,
        )

    app.include_router(sessions_router)
    app.include_router(runs_router)
    app.include_router(model_profiles_router)
    app.include_router(datasources_router)
    return app
