"""Management-only version history and immutable restoration endpoints."""

from contracts.datalink import (
    DataLinkCatalogRead,
    DataLinkErrorCode,
    DataLinkNodeType,
    DataLinkRebuildConflictsRead,
    DataLinkResolveCandidateRequest,
    DataLinkRestoreRequest,
    DataLinkVersionDiffRead,
    DataLinkVersionPublishRead,
    DataLinkVersionsRead,
)
from fastapi import APIRouter, Query, Request

from server.errors import DataLinkServiceError
from server.revisions import RevisionConflict

router = APIRouter(prefix="/v1/graphs/{datasource_id}")


@router.get("/versions", response_model=DataLinkVersionsRead)
def versions(
    datasource_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
):
    return request.app.state.version_history.list_versions(
        datasource_id, page=page, page_size=page_size
    )


@router.post("/restore", response_model=DataLinkVersionPublishRead)
def restore(datasource_id: str, payload: DataLinkRestoreRequest, request: Request):
    try:
        return request.app.state.version_history.restore(datasource_id, payload)
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc


@router.get("/versions/{version}/conflicts", response_model=DataLinkRebuildConflictsRead)
def conflicts(datasource_id: str, version: str, request: Request):
    try:
        return request.app.state.version_history.list_conflicts(datasource_id, version)
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc


@router.get("/versions/{version}/diff", response_model=DataLinkVersionDiffRead)
def version_diff(datasource_id: str, version: str, request: Request):
    try:
        return request.app.state.version_history.diff_version(datasource_id, version)
    except RevisionConflict as exc:
        status = 404 if exc.code == DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND else 409
        raise DataLinkServiceError(exc.code, str(exc), status_code=status) from exc


@router.post("/resolve-candidate", response_model=DataLinkVersionPublishRead)
def resolve_candidate(
    datasource_id: str, payload: DataLinkResolveCandidateRequest, request: Request
):
    try:
        return request.app.state.version_history.resolve_candidate(datasource_id, payload)
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc


@router.get("/versions/{version}/catalog", response_model=DataLinkCatalogRead)
def candidate_catalog(
    datasource_id: str,
    version: str,
    request: Request,
    node_type: DataLinkNodeType | None = None,
    query: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
):
    try:
        snapshot = request.app.state.version_history.candidate_snapshot(datasource_id, version)
        catalog = request.app.state.graph_explorer.catalog_snapshot(snapshot)
        return catalog.list(
            datasource_id, version, node_type=node_type, query=query, page=page, page_size=page_size
        )
    except RevisionConflict as exc:
        raise DataLinkServiceError(exc.code, str(exc), status_code=409) from exc
