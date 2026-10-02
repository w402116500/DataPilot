from __future__ import annotations

from typing import Annotated

from contracts.datalink import (
    DataLinkBuildRead,
    DataLinkCatalogDetailRead,
    DataLinkCatalogRead,
    DataLinkDraftExploreRead,
    DataLinkDraftPreviewRequest,
    DataLinkDraftRead,
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkExploreRequest,
    DataLinkGraphEntriesRead,
    DataLinkGraphEntryType,
    DataLinkGraphRead,
    DataLinkHealthRead,
    DataLinkNodeType,
    DataLinkPublishRead,
    DataLinkPublishRequest,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    DataLinkRelationDetailRead,
    DataLinkRelationsRead,
    DataLinkRemoveResult,
    DataLinkStatusRead,
    DataLinkSubgraphRead,
)
from fastapi import APIRouter, BackgroundTasks, Query, Request

from server.builder.service import (
    BuildAlreadyRunningError,
    BuildExecutionError,
    DataLinkBuildService,
)
from server.config import Settings
from server.errors import DataLinkServiceError
from server.graph.repository import GraphBuildRecord, GraphRepository
from server.graph.storage import GraphStorage
from server.retrieval.explore import GraphAccessError, GraphExplorer
from server.revisions import RevisionConflict

router = APIRouter()


def _graph_database_status(storage: GraphStorage) -> str:
    """只通过图谱存储所有者检查 SQLite，避免 health 另开一套连接规则。"""

    return "ok" if storage.check() else "unavailable"


def _source_root_status(settings: Settings) -> str:
    """只返回受控根目录是否可用，不把真实路径写入响应。"""

    return "ok" if settings.source_root.is_dir() else "missing"


@router.get("/health", response_model=DataLinkHealthRead)
async def health(request: Request) -> DataLinkHealthRead:
    """报告独立服务的基础可用性，不调用模型也不泄露任何配置值。"""

    settings: Settings = request.app.state.settings
    storage: GraphStorage = request.app.state.graph_storage
    graph_database = _graph_database_status(storage)
    if graph_database != "ok":
        raise DataLinkServiceError(
            DataLinkErrorCode.DATALINK_UNAVAILABLE,
            "Graph database is unavailable",
            status_code=503,
        )

    source_root = _source_root_status(settings)
    # A connection-only deployment does not require a local file root.
    source_root = "ok" if source_root == "missing" and settings.service_token else source_root
    return DataLinkHealthRead(
        status="healthy" if source_root == "ok" else "degraded",
        graph_database=graph_database,
        source_root=source_root,
        model_configured=settings.model_configured,
        mcp_mounted=bool(request.app.state.mcp_mounted),
    )


@router.post(
    "/v1/graphs/rebuild",
    status_code=202,
    response_model=DataLinkRebuildResult,
)
def rebuild_graph(
    request: Request,
    payload: DataLinkRebuildRequest,
    background_tasks: BackgroundTasks,
) -> DataLinkRebuildResult:
    """认领后台 Build 并立即返回；完成状态只能经 status 接口确认。"""

    builder: DataLinkBuildService = request.app.state.build_service
    try:
        submission = builder.submit_rebuild(payload)
        if submission.claim is not None:
            background_tasks.add_task(builder.execute_submitted_rebuild, payload, submission.claim)
        return submission.result
    except BuildAlreadyRunningError as exc:
        raise DataLinkServiceError(
            DataLinkErrorCode.BUILD_ALREADY_RUNNING,
            "Graph build is already running",
            status_code=409,
            details={"build_id": exc.build.id},
        ) from exc
    except BuildExecutionError as exc:
        raise DataLinkServiceError(
            exc.build.error_code or DataLinkErrorCode.BUILD_FAILED,
            exc.build.error_message or "Graph build failed",
            status_code=409,
            details={"build_id": exc.build.id},
        ) from exc


@router.get("/v1/graphs/{datasource_id}/status", response_model=DataLinkStatusRead)
def graph_status(datasource_id: str, request: Request) -> DataLinkStatusRead:
    """供主后端在超时或重启后安全对账当前版本和最近一次 Build。"""

    repository: GraphRepository = request.app.state.graph_repository
    head = repository.get_head(datasource_id)
    build = repository.get_latest_build(datasource_id)
    published = repository.get_build(head.current_build_id) if head else None
    return DataLinkStatusRead(
        datasource_id=datasource_id,
        current_graph_version=head.current_graph_version if head else None,
        head_build=_build_read(published) if published else None,
        current_build=_build_read(build) if build else None,
        last_error_code=build.error_code if build and build.status.value == "failed" else None,
        last_error_message=build.error_message
        if build and build.status.value == "failed"
        else None,
    )


@router.get("/v1/graphs/{datasource_id}", response_model=DataLinkGraphRead)
def read_graph(
    datasource_id: str, request: Request, graph_version: str | None = None
) -> DataLinkGraphRead:
    """读取当前 Head 或指定已完成版本，不允许前端接触运行中中间结果。"""

    repository: GraphRepository = request.app.state.graph_repository
    explorer: GraphExplorer = request.app.state.graph_explorer
    resolved_version = _resolve_graph_version(repository, datasource_id, graph_version)
    try:
        return explorer.read_graph(datasource_id, resolved_version)
    except GraphAccessError as exc:
        raise DataLinkServiceError(
            exc.code,
            exc.message,
            status_code=409 if exc.code == DataLinkErrorCode.DATASOURCE_MISMATCH else 404,
        ) from exc


@router.get(
    "/v1/graphs/{datasource_id}/entries",
    response_model=DataLinkGraphEntriesRead,
)
def list_graph_entries(
    datasource_id: str,
    request: Request,
    graph_version: str | None = Query(default=None, min_length=1, max_length=120),
    entry_type: DataLinkGraphEntryType | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> DataLinkGraphEntriesRead:
    """列出当前完成版本中的表和实体，作为局部图的受控根节点入口。"""

    repository: GraphRepository = request.app.state.graph_repository
    explorer: GraphExplorer = request.app.state.graph_explorer
    resolved_version = _resolve_graph_version(repository, datasource_id, graph_version)
    try:
        return explorer.list_browser_entries(
            datasource_id,
            resolved_version,
            entry_type=entry_type,
            query=query,
            page=page,
            page_size=page_size,
        )
    except GraphAccessError as exc:
        raise _graph_access_error(exc) from exc


@router.get(
    "/v1/graphs/{datasource_id}/subgraph",
    response_model=DataLinkSubgraphRead,
)
def read_graph_subgraph(
    datasource_id: str,
    request: Request,
    root_node_id: str = Query(min_length=1, max_length=300),
    graph_version: str | None = Query(default=None, min_length=1, max_length=120),
    edge_types: Annotated[list[DataLinkEdgeType] | None, Query()] = None,
    hops: int = Query(default=1, ge=1, le=2),
) -> DataLinkSubgraphRead:
    """读取一个完成版本内一到两跳的局部图，不把整张大图伪装成浏览结果。"""

    repository: GraphRepository = request.app.state.graph_repository
    explorer: GraphExplorer = request.app.state.graph_explorer
    resolved_version = _resolve_graph_version(repository, datasource_id, graph_version)
    try:
        return explorer.read_browser_subgraph(
            datasource_id,
            resolved_version,
            root_node_id=root_node_id,
            edge_types=edge_types,
            hops=hops,
        )
    except GraphAccessError as exc:
        raise _graph_access_error(exc) from exc


@router.get("/v1/graphs/{datasource_id}/catalog", response_model=DataLinkCatalogRead)
def list_catalog(
    datasource_id: str,
    request: Request,
    graph_version: str = Query(min_length=1, max_length=120),
    node_type: Annotated[DataLinkNodeType | None, Query(alias="type")] = None,
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> DataLinkCatalogRead:
    try:
        catalog = request.app.state.graph_explorer.catalog(datasource_id, graph_version)
        return catalog.list(
            datasource_id,
            graph_version,
            node_type=node_type,
            query=query,
            page=page,
            page_size=page_size,
        )
    except GraphAccessError as exc:
        raise _graph_access_error(exc) from exc


@router.get(
    "/v1/graphs/{datasource_id}/catalog/{node_id:path}", response_model=DataLinkCatalogDetailRead
)
def catalog_detail(
    datasource_id: str,
    node_id: str,
    request: Request,
    graph_version: str = Query(min_length=1, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> DataLinkCatalogDetailRead:
    try:
        catalog = request.app.state.graph_explorer.catalog(datasource_id, graph_version)
        if node_id not in catalog.nodes:
            raise GraphAccessError(
                DataLinkErrorCode.INVALID_QUERY, "Catalog node is unavailable in this version"
            )
        return catalog.detail(datasource_id, graph_version, node_id, page=page, page_size=page_size)
    except GraphAccessError as exc:
        raise _graph_access_error(exc) from exc


@router.get("/v1/graphs/{datasource_id}/relations", response_model=DataLinkRelationsRead)
def list_relations(
    datasource_id: str,
    request: Request,
    graph_version: str = Query(min_length=1, max_length=120),
    node_id: str | None = Query(default=None, min_length=1, max_length=300),
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> DataLinkRelationsRead:
    try:
        catalog = request.app.state.graph_explorer.catalog(datasource_id, graph_version)
        if node_id is not None and node_id not in catalog.nodes:
            raise GraphAccessError(
                DataLinkErrorCode.INVALID_QUERY, "Catalog node is unavailable in this version"
            )
        return catalog.list_relations(
            datasource_id,
            graph_version,
            node_id=node_id,
            query=query,
            page=page,
            page_size=page_size,
        )
    except GraphAccessError as exc:
        raise _graph_access_error(exc) from exc


@router.get(
    "/v1/graphs/{datasource_id}/relations/{relation_id:path}",
    response_model=DataLinkRelationDetailRead,
)
def relation_detail(
    datasource_id: str,
    relation_id: str,
    request: Request,
    graph_version: str = Query(min_length=1, max_length=120),
) -> DataLinkRelationDetailRead:
    try:
        catalog = request.app.state.graph_explorer.catalog(datasource_id, graph_version)
        item = catalog.relation(relation_id)
        if item is None:
            raise GraphAccessError(
                DataLinkErrorCode.INVALID_QUERY, "Relation is unavailable in this version"
            )
        return DataLinkRelationDetailRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            item=item,
        )
    except GraphAccessError as exc:
        raise _graph_access_error(exc) from exc


@router.post("/v1/graphs/{datasource_id}/remove", response_model=DataLinkRemoveResult)
def remove_graph(datasource_id: str, request: Request) -> DataLinkRemoveResult:
    """幂等删除本服务全部版本化图谱记录，不删除上传副本。"""

    repository: GraphRepository = request.app.state.graph_repository
    return DataLinkRemoveResult(
        datasource_id=datasource_id,
        removed=repository.remove_datasource(datasource_id),
    )


@router.get("/v1/graphs/{datasource_id}/draft", response_model=DataLinkDraftRead | None)
def get_draft(datasource_id: str, request: Request) -> DataLinkDraftRead | None:
    return request.app.state.revision_service.get_draft(datasource_id)


@router.put("/v1/graphs/{datasource_id}/draft", response_model=DataLinkDraftRead)
def save_draft(
    datasource_id: str, payload: DataLinkDraftSaveRequest, request: Request
) -> DataLinkDraftRead:
    try:
        return request.app.state.revision_service.save_draft(datasource_id, payload)
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc


@router.post("/v1/graphs/{datasource_id}/publish", response_model=DataLinkPublishRead)
def publish_draft(
    datasource_id: str, payload: DataLinkPublishRequest, request: Request
) -> DataLinkPublishRead:
    try:
        return request.app.state.revision_service.publish(datasource_id, payload)
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc


@router.post("/v1/graphs/{datasource_id}/draft/preview", response_model=DataLinkDraftExploreRead)
def preview_draft(
    datasource_id: str, payload: DataLinkDraftPreviewRequest, request: Request
) -> DataLinkDraftExploreRead:
    try:
        snapshot = request.app.state.revision_service.preview_snapshot(
            datasource_id, payload.expected_draft_revision
        )
        result = request.app.state.graph_explorer.explore_snapshot(
            snapshot,
            DataLinkExploreRequest(
                datasource_id=datasource_id,
                graph_version=snapshot.build.graph_version,
                query=payload.query,
                focus=payload.focus,
                max_nodes=payload.max_nodes,
            ),
        )
        return DataLinkDraftExploreRead(
            datasource_id=datasource_id,
            base_graph_version=snapshot.build.graph_version,
            schema_revision=snapshot.build.schema_revision,
            draft_revision=payload.expected_draft_revision,
            result=result,
        )
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc


def _resolve_graph_version(
    repository: GraphRepository,
    datasource_id: str,
    graph_version: str | None,
) -> str:
    """缺省时只解析当前 Graph Head，随后仍由 Explorer 校验完成版本归属。"""

    if graph_version is not None:
        return graph_version
    head = repository.get_head(datasource_id)
    if head is None:
        raise DataLinkServiceError(
            DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND,
            "No completed graph version is available",
            status_code=404,
        )
    return head.current_graph_version


def _graph_access_error(exc: GraphAccessError) -> DataLinkServiceError:
    """将图版本、根节点等受控访问失败映射为独立服务统一错误信封。"""

    return DataLinkServiceError(
        exc.code,
        exc.message,
        status_code=(
            409
            if exc.code in {DataLinkErrorCode.DATASOURCE_MISMATCH, DataLinkErrorCode.INVALID_QUERY}
            else 404
        ),
    )


def _build_read(build: GraphBuildRecord) -> DataLinkBuildRead:
    """把内部 Build 记录压缩成主后端可对账的共享 DTO。"""

    return DataLinkBuildRead(
        build_id=build.id,
        datasource_id=build.datasource_id,
        schema_revision=build.schema_revision,
        connection_revision=build.connection_revision,
        attempt_no=build.attempt_no,
        status=build.status,
        graph_version=build.graph_version,
        origin_kind=build.origin_kind,
        publication_state=build.publication_state,
        base_graph_version=build.base_graph_version,
        error_code=build.error_code,
        error_message=build.error_message,
        started_at=build.started_at,
        finished_at=build.finished_at,
        created_at=build.created_at,
    )
