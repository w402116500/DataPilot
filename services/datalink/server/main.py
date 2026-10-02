from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from contracts.errors import AppError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from server.api.routes import router as api_router
from server.api.versions import router as versions_router
from server.builder.service import DataLinkBuildService
from server.config import Settings, load_settings
from server.errors import (
    DataLinkServiceError,
    handle_datalink_error,
    handle_unexpected_error,
    handle_validation_error,
)
from server.graph.repository import GraphRepository
from server.graph.storage import GraphStorage
from server.mapper.client import OpenAICompatibleSemanticClient, SemanticModelClient
from server.mcp.server import create_mcp_server
from server.retrieval.explore import GraphExplorer
from server.revisions import RevisionService
from server.version_history import VersionHistoryService


def create_app(
    settings: Settings | None = None, *, semantic_client: SemanticModelClient | None = None
) -> FastAPI:
    """装配独立 DataLink 服务，并允许测试注入不联网的受控模型端口。"""

    resolved_settings = settings or load_settings()
    graph_storage = GraphStorage(resolved_settings.database_path)
    graph_repository = GraphRepository(graph_storage)
    resolved_model_client = semantic_client or OpenAICompatibleSemanticClient(resolved_settings)
    build_service = DataLinkBuildService(resolved_settings, graph_repository, resolved_model_client)
    graph_explorer = GraphExplorer(graph_repository, resolved_model_client)
    revision_service = RevisionService(graph_storage, graph_repository)
    mcp_server = create_mcp_server(graph_explorer)
    mcp_app = mcp_server.http_app(path="/", transport="streamable-http")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """按顺序启动图谱库和 FastMCP 会话管理器。"""

        graph_repository.initialize()
        graph_repository.recover_interrupted_builds()
        async with mcp_app.lifespan(app):
            yield

    app = FastAPI(title="DataPilot DataLink", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.graph_storage = graph_storage
    app.state.graph_repository = graph_repository
    app.state.build_service = build_service
    app.state.graph_explorer = graph_explorer
    app.state.revision_service = revision_service
    app.state.version_history = VersionHistoryService(graph_storage, graph_repository)
    app.state.mcp_mounted = False

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        """为 REST 错误信封提供请求标识，不信任外部传入的具体内容。"""

        request.state.request_id = f"req_{uuid4().hex}"
        return await call_next(request)

    app.add_exception_handler(DataLinkServiceError, handle_datalink_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(AppError, handle_unexpected_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    app.include_router(api_router)
    app.include_router(versions_router)

    app.state.mcp_server = mcp_server
    app.mount("/mcp", mcp_app)
    app.state.mcp_mounted = True
    return app


app = create_app()
