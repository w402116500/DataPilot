from __future__ import annotations

from typing import Annotated

from agent_runtime.ports import CancellationSignal
from application.datalink_preview import DataLinkPreviewService
from application.datalink_validation import DataLinkValidationService
from application.datalink_versions import DataLinkVersionsService
from application.datasources import DataSourceService
from contracts.api import ApiEnvelope, Page, PageResult
from contracts.datalink import (
    DataLinkCatalogDetailRead,
    DataLinkCatalogRead,
    DataLinkConnectionGrantConsumeRequest,
    DataLinkConnectionGrantRead,
    DataLinkDraftPreviewRead,
    DataLinkDraftPreviewRequest,
    DataLinkDraftRead,
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkGraphEntriesRead,
    DataLinkGraphEntryType,
    DataLinkGraphRead,
    DataLinkNodeType,
    DataLinkPreviewRead,
    DataLinkPreviewRequest,
    DataLinkPublishRead,
    DataLinkPublishRequest,
    DataLinkRebuildConflictsRead,
    DataLinkRebuildResult,
    DataLinkRelationsRead,
    DataLinkResolveCandidateRequest,
    DataLinkRestoreRequest,
    DataLinkStatusRead,
    DataLinkSubgraphRead,
    DataLinkVersionDiffRead,
    DataLinkVersionPublishRead,
    DataLinkVersionsRead,
)
from contracts.datasources import (
    DataSourceConnectionCreate,
    DataSourceConnectionUpdate,
    DataSourceDeleteResult,
    DataSourceDescriptionUpdate,
    DataSourceMaskFieldsUpdate,
    DataSourceRead,
    DataSourceTypeList,
    SchemaSummaryRead,
    TableDataRead,
)
from contracts.errors import AppError, ErrorCode
from contracts.validation import DataLinkValidationRead, DataLinkValidationRequest
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
)

from server.dependencies import (
    get_datalink_preview_service,
    get_datalink_validation_service,
    get_datalink_versions_service,
    get_datasource_service,
    get_request_cancellation,
    inspect_datasource_in_background,
)
from server.responses import success_response

router = APIRouter(tags=["datasources"])
DataSourceServiceDep = Annotated[DataSourceService, Depends(get_datasource_service)]
DataLinkVersionsDep = Annotated[DataLinkVersionsService, Depends(get_datalink_versions_service)]
DataLinkValidationDep = Annotated[
    DataLinkValidationService, Depends(get_datalink_validation_service)
]


@router.post(
    "/internal/connection-grants/consume",
    response_model=ApiEnvelope[DataLinkConnectionGrantRead],
    include_in_schema=False,
)
async def consume_connection_grant(
    request: Request,
    payload: DataLinkConnectionGrantConsumeRequest,
    service: DataSourceServiceDep,
    authorization: Annotated[str | None, Header()] = None,
):
    """DataLink 专用内部授权消费端点；不进入公开 OpenAPI。"""

    expected = request.app.state.settings.datalink_service_token
    if expected is None or authorization != f"Bearer {expected.get_secret_value()}":
        raise AppError(ErrorCode.SECURITY_POLICY_VIOLATION, "内部授权无效", status_code=403)
    grant = await service.connection_service().consume(payload)
    return success_response(request, grant)


@router.post(
    "/datasources/{datasource_id}/datalink/validations",
    response_model=ApiEnvelope[DataLinkValidationRead],
)
async def create_datalink_validation(
    request: Request,
    datasource_id: str,
    payload: DataLinkValidationRequest,
    service: DataLinkValidationDep,
    cancellation: Annotated[CancellationSignal, Depends(get_request_cancellation)],
):
    return success_response(
        request,
        await service.create(datasource_id, payload, cancellation),
        status_code=202,
    )


@router.get(
    "/datasources/{datasource_id}/datalink/validations",
    response_model=ApiEnvelope[list[DataLinkValidationRead]],
)
async def list_datalink_validations(
    request: Request, datasource_id: str, service: DataLinkValidationDep
):
    return success_response(request, await service.list(datasource_id))


@router.get(
    "/datasources/{datasource_id}/datalink/validations/{validation_id}",
    response_model=ApiEnvelope[DataLinkValidationRead],
)
async def get_datalink_validation(
    request: Request,
    datasource_id: str,
    validation_id: str,
    service: DataLinkValidationDep,
):
    return success_response(request, await service.get(datasource_id, validation_id))


@router.post(
    "/datasources/{datasource_id}/datalink/validations/{validation_id}/cancel",
    response_model=ApiEnvelope[DataLinkValidationRead],
)
async def cancel_datalink_validation(
    request: Request,
    datasource_id: str,
    validation_id: str,
    service: DataLinkValidationDep,
):
    return success_response(request, await service.cancel(datasource_id, validation_id))


@router.get(
    "/datasources/{datasource_id}/datalink/versions",
    response_model=ApiEnvelope[DataLinkVersionsRead],
)
async def datalink_versions(
    request: Request,
    datasource_id: str,
    service: DataLinkVersionsDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
):
    return success_response(
        request, await service.versions(datasource_id, page=page, page_size=page_size)
    )


@router.post(
    "/datasources/{datasource_id}/datalink/restore",
    response_model=ApiEnvelope[DataLinkVersionPublishRead],
)
async def restore_datalink_version(
    request: Request,
    datasource_id: str,
    payload: DataLinkRestoreRequest,
    service: DataLinkVersionsDep,
):
    return success_response(request, await service.restore(datasource_id, payload))


@router.get(
    "/datasources/{datasource_id}/datalink/versions/{version}/conflicts",
    response_model=ApiEnvelope[DataLinkRebuildConflictsRead],
)
async def datalink_version_conflicts(
    request: Request, datasource_id: str, version: str, service: DataLinkVersionsDep
):
    return success_response(request, await service.conflicts(datasource_id, version))


@router.post(
    "/datasources/{datasource_id}/datalink/resolve-candidate",
    response_model=ApiEnvelope[DataLinkVersionPublishRead],
)
async def resolve_datalink_candidate(
    request: Request,
    datasource_id: str,
    payload: DataLinkResolveCandidateRequest,
    service: DataLinkVersionsDep,
):
    return success_response(request, await service.resolve(datasource_id, payload))


@router.get(
    "/datasources/{datasource_id}/datalink/versions/{version}/diff",
    response_model=ApiEnvelope[DataLinkVersionDiffRead],
)
async def datalink_version_diff(
    request: Request, datasource_id: str, version: str, service: DataLinkVersionsDep
):
    return success_response(request, await service.diff(datasource_id, version))


@router.get(
    "/datasources/{datasource_id}/datalink/versions/{version}/catalog",
    response_model=ApiEnvelope[DataLinkCatalogRead],
)
async def datalink_candidate_catalog(
    request: Request,
    datasource_id: str,
    version: str,
    service: DataLinkVersionsDep,
    node_type: DataLinkNodeType | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
):
    return success_response(
        request,
        await service.catalog(
            datasource_id, version, node_type=node_type, page=page, page_size=page_size
        ),
    )


@router.post(
    "/datasources/{datasource_id}/datalink/preview", response_model=ApiEnvelope[DataLinkPreviewRead]
)
async def preview_datalink(
    request: Request,
    datasource_id: str,
    payload: DataLinkPreviewRequest,
    service: Annotated[DataLinkPreviewService, Depends(get_datalink_preview_service)],
    cancellation: Annotated[CancellationSignal, Depends(get_request_cancellation)],
):
    return success_response(request, await service.preview(datasource_id, payload, cancellation))


@router.post(
    "/datasources/{datasource_id}/datalink/draft/preview",
    response_model=ApiEnvelope[DataLinkDraftPreviewRead],
)
async def preview_datalink_draft(
    request: Request,
    datasource_id: str,
    payload: DataLinkDraftPreviewRequest,
    service: Annotated[DataLinkPreviewService, Depends(get_datalink_preview_service)],
):
    return success_response(request, await service.preview_draft(datasource_id, payload))


@router.get("/datasource-types", response_model=ApiEnvelope[DataSourceTypeList])
async def list_datasource_types(
    request: Request,
    service: DataSourceServiceDep,
):
    """列出当前可创建的数据源类型及其允许的文件扩展名。"""

    return success_response(request, {"items": service.supported_types()})


@router.get("/datasources", response_model=ApiEnvelope[PageResult[DataSourceRead]])
async def list_datasources(
    request: Request,
    service: DataSourceServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """分页读取数据源及其检查状态。"""

    return success_response(request, await service.list(Page(page=page, page_size=page_size)))


@router.post("/datasources/upload", status_code=202, response_model=ApiEnvelope[DataSourceRead])
async def upload_datasource(
    request: Request,
    background_tasks: BackgroundTasks,
    service: DataSourceServiceDep,
    file: Annotated[UploadFile, File()],
    type_name: Annotated[str, Form(alias="type")],
    name: Annotated[str, Form(min_length=1, max_length=200)],
    description: Annotated[str | None, Form(max_length=2_000)] = None,
):
    """保存上传文件并立即返回检查中状态，文件检查由独立后台事务完成。"""

    datasource = await service.upload(
        name=name,
        description=description,
        type_name=type_name,
        original_filename=file.filename,
        content=file.file,
    )
    await service.commit()
    background_tasks.add_task(
        inspect_datasource_in_background,
        request.app.state.session_factory,
        request.app.state.settings,
        datasource.id,
        request.app.state.datalink_client,
    )
    return success_response(request, datasource, status_code=202)


@router.post(
    "/datasources/connections", status_code=202, response_model=ApiEnvelope[DataSourceRead]
)
async def create_connection_datasource(
    request: Request,
    background_tasks: BackgroundTasks,
    service: DataSourceServiceDep,
    payload: DataSourceConnectionCreate,
):
    datasource = await service.create_connection(payload)
    await service.commit()
    background_tasks.add_task(
        inspect_datasource_in_background,
        request.app.state.session_factory,
        request.app.state.settings,
        datasource.id,
        request.app.state.datalink_client,
    )
    return success_response(request, datasource, status_code=202)


@router.patch("/datasources/{datasource_id}/connection", response_model=ApiEnvelope[DataSourceRead])
async def update_connection_datasource(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
    payload: DataSourceConnectionUpdate,
):
    return success_response(request, await service.update_connection(datasource_id, payload))


@router.get("/datasources/{datasource_id}", response_model=ApiEnvelope[DataSourceRead])
async def get_datasource(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
):
    """读取数据源详情，包括当前 Schema revision 和安全错误信息。"""

    return success_response(request, await service.get(datasource_id))


@router.post(
    "/datasources/{datasource_id}/test",
    status_code=202,
    response_model=ApiEnvelope[DataSourceRead],
)
async def test_datasource(
    request: Request,
    background_tasks: BackgroundTasks,
    datasource_id: str,
    service: DataSourceServiceDep,
):
    """手动重新检查未删除的数据源，接口不等待长耗时文件扫描结束。"""

    datasource = await service.retry(datasource_id)
    await service.commit()
    background_tasks.add_task(
        inspect_datasource_in_background,
        request.app.state.session_factory,
        request.app.state.settings,
        datasource.id,
        request.app.state.datalink_client,
    )
    return success_response(request, datasource, status_code=202)


@router.get("/datasources/{datasource_id}/schema", response_model=ApiEnvelope[SchemaSummaryRead])
async def get_datasource_schema(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
):
    """返回上次检查固化的 Schema，不会临时扫描或暴露原始文件。"""

    return success_response(request, await service.get_schema(datasource_id))


@router.patch(
    "/datasources/{datasource_id}/mask-fields",
    response_model=ApiEnvelope[DataSourceRead],
)
async def update_datasource_mask_fields(
    request: Request,
    datasource_id: str,
    payload: DataSourceMaskFieldsUpdate,
    service: DataSourceServiceDep,
):
    """保存用户确认的遮蔽列清单，空数组也代表一次明确确认。"""

    return success_response(request, await service.update_mask_fields(datasource_id, payload))


@router.patch(
    "/datasources/{datasource_id}/description",
    response_model=ApiEnvelope[DataSourceRead],
)
async def update_datasource_description(
    request: Request,
    datasource_id: str,
    payload: DataSourceDescriptionUpdate,
    service: DataSourceServiceDep,
):
    """保存数据源补充说明，长度与上传表单一致。"""

    return success_response(request, await service.update_description(datasource_id, payload))


@router.get(
    "/datasources/{datasource_id}/tables/{table_name}/preview",
    response_model=ApiEnvelope[TableDataRead],
)
async def preview_table(
    request: Request,
    datasource_id: str,
    table_name: str,
    service: DataSourceServiceDep,
    limit: int = Query(default=50, ge=1),
):
    """预览 Schema 白名单中的单表，行数始终限制在 50 以内。"""

    preview = await service.preview(datasource_id, table_name, min(limit, 50))
    return success_response(request, preview)


@router.delete("/datasources/{datasource_id}", response_model=ApiEnvelope[DataSourceDeleteResult])
async def delete_datasource(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
):
    """清理 DataLink 图谱后写入 tombstone；历史审计和 Artifact 保留。"""

    return success_response(request, await service.delete(datasource_id))


@router.post(
    "/datasources/{datasource_id}/datalink/rebuild",
    status_code=202,
    response_model=ApiEnvelope[DataLinkRebuildResult],
)
async def rebuild_datasource_datalink(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
):
    """受理后台 DataLink 重建；完成版本由后续状态对账后才切换。"""

    return success_response(
        request,
        await service.rebuild_datalink(datasource_id),
        status_code=202,
    )


@router.get(
    "/datasources/{datasource_id}/datalink/status",
    response_model=ApiEnvelope[DataLinkStatusRead],
)
async def get_datasource_datalink_status(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
):
    """返回 DataLink Build 状态，并在必要时对账主后端的 ready 投影。"""

    return success_response(request, await service.datalink_status(datasource_id))


@router.get(
    "/datasources/{datasource_id}/datalink/graph",
    response_model=ApiEnvelope[DataLinkGraphRead],
)
async def get_datasource_datalink_graph(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
    graph_version: str | None = Query(default=None, min_length=1, max_length=120),
):
    """经主后端读取当前或固定版本的 DataLink 图谱面板数据。"""

    graph = await service.datalink_graph(datasource_id, graph_version=graph_version)
    return success_response(request, graph)


@router.get(
    "/datasources/{datasource_id}/datalink/entries",
    response_model=ApiEnvelope[DataLinkGraphEntriesRead],
)
async def get_datasource_datalink_entries(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
    graph_version: str | None = Query(default=None, min_length=1, max_length=120),
    entry_type: DataLinkGraphEntryType | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    """读取当前完成图谱中的表或实体入口，供数据地图选择根节点。"""

    entries = await service.datalink_graph_entries(
        datasource_id,
        graph_version=graph_version,
        entry_type=entry_type,
        query=query,
        page=page,
        page_size=page_size,
    )
    return success_response(request, entries)


@router.get(
    "/datasources/{datasource_id}/datalink/subgraph",
    response_model=ApiEnvelope[DataLinkSubgraphRead],
)
async def get_datasource_datalink_subgraph(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
    root_node_id: str = Query(min_length=1, max_length=300),
    graph_version: str | None = Query(default=None, min_length=1, max_length=120),
    edge_types: Annotated[list[DataLinkEdgeType] | None, Query()] = None,
    hops: int = Query(default=1, ge=1, le=2),
):
    """读取当前根节点附近一到两跳的局部图，边类型使用重复查询参数。"""

    subgraph = await service.datalink_subgraph(
        datasource_id,
        root_node_id=root_node_id,
        graph_version=graph_version,
        edge_types=edge_types,
        hops=hops,
    )
    return success_response(request, subgraph)


@router.get(
    "/datasources/{datasource_id}/datalink/catalog", response_model=ApiEnvelope[DataLinkCatalogRead]
)
async def get_datalink_catalog(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
    graph_version: str | None = None,
    type: DataLinkNodeType | None = None,
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    return success_response(
        request,
        await service.datalink_catalog(
            datasource_id,
            graph_version=graph_version,
            node_type=type,
            query=query,
            page=page,
            page_size=page_size,
        ),
    )


@router.get(
    "/datasources/{datasource_id}/datalink/catalog/{node_id:path}",
    response_model=ApiEnvelope[DataLinkCatalogDetailRead],
)
async def get_datalink_catalog_detail(
    request: Request,
    datasource_id: str,
    node_id: str,
    service: DataSourceServiceDep,
    graph_version: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    return success_response(
        request,
        await service.datalink_catalog_detail(
            datasource_id, node_id, graph_version=graph_version, page=page, page_size=page_size
        ),
    )


@router.get(
    "/datasources/{datasource_id}/datalink/relations",
    response_model=ApiEnvelope[DataLinkRelationsRead],
)
async def get_datalink_relations(
    request: Request,
    datasource_id: str,
    service: DataSourceServiceDep,
    graph_version: str | None = None,
    node_id: str | None = Query(default=None, min_length=1, max_length=300),
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    return success_response(
        request,
        await service.datalink_relations(
            datasource_id,
            graph_version=graph_version,
            node_id=node_id,
            query=query,
            page=page,
            page_size=page_size,
        ),
    )


@router.get(
    "/datasources/{datasource_id}/datalink/draft",
    response_model=ApiEnvelope[DataLinkDraftRead | None],
)
async def get_datalink_draft(request: Request, datasource_id: str, service: DataSourceServiceDep):
    return success_response(request, await service.datalink_draft(datasource_id))


@router.put(
    "/datasources/{datasource_id}/datalink/draft",
    response_model=ApiEnvelope[DataLinkDraftRead],
)
async def save_datalink_draft(
    request: Request,
    datasource_id: str,
    payload: DataLinkDraftSaveRequest,
    service: DataSourceServiceDep,
):
    return success_response(request, await service.save_datalink_draft(datasource_id, payload))


@router.post(
    "/datasources/{datasource_id}/datalink/publish",
    response_model=ApiEnvelope[DataLinkPublishRead],
)
async def publish_datalink(
    request: Request,
    datasource_id: str,
    payload: DataLinkPublishRequest,
    service: DataSourceServiceDep,
):
    return success_response(request, await service.publish_datalink(datasource_id, payload))
