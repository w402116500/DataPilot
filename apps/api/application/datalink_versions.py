"""Data source scoped DataLink version administration through the REST port."""

from contracts.datalink import (
    DataLinkCatalogRead,
    DataLinkRebuildConflictsRead,
    DataLinkResolveCandidateRequest,
    DataLinkRestoreRequest,
    DataLinkVersionDiffRead,
    DataLinkVersionPublishRead,
    DataLinkVersionsRead,
)
from contracts.errors import AppError, ErrorCode

from application.datalink_client import DataLinkClientError, DataLinkManagementPort
from application.datasources import DataSourceService


class DataLinkVersionsService:
    def __init__(self, datasources: DataSourceService, client: DataLinkManagementPort) -> None:
        self.datasources = datasources
        self.client = client

    async def versions(
        self, datasource_id: str, *, page: int, page_size: int
    ) -> DataLinkVersionsRead:
        await self.datasources.get_readable(datasource_id)
        try:
            return await self.client.versions(datasource_id, page=page, page_size=page_size)
        except DataLinkClientError as exc:
            raise DataSourceService._to_app_error(exc) from exc

    async def conflicts(self, datasource_id: str, version: str) -> DataLinkRebuildConflictsRead:
        source = await self.datasources.get_readable(datasource_id)
        try:
            result = await self.client.rebuild_conflicts(datasource_id, version)
        except DataLinkClientError as exc:
            raise DataSourceService._to_app_error(exc) from exc
        self._require_schema(source.schema_revision, result.schema_revision)
        return result

    async def diff(self, datasource_id: str, version: str) -> DataLinkVersionDiffRead:
        await self.datasources.get_readable(datasource_id)
        try:
            return await self.client.version_diff(datasource_id, version)
        except DataLinkClientError as exc:
            raise DataSourceService._to_app_error(exc) from exc

    async def catalog(
        self, datasource_id: str, version: str, *, node_type: str | None, page: int, page_size: int
    ) -> DataLinkCatalogRead:
        # Candidate metadata verifies Schema before allowing conflict rebinding.
        await self.conflicts(datasource_id, version)
        try:
            return await self.client.version_catalog(
                datasource_id, version, node_type=node_type, page=page, page_size=page_size
            )
        except DataLinkClientError as exc:
            raise DataSourceService._to_app_error(exc) from exc

    async def restore(
        self, datasource_id: str, payload: DataLinkRestoreRequest
    ) -> DataLinkVersionPublishRead:
        source = await self.datasources.get_readable(datasource_id)
        self._require_schema(source.schema_revision, payload.schema_revision)
        try:
            result = await self.client.restore(datasource_id, payload)
        except DataLinkClientError as exc:
            raise DataSourceService._to_app_error(exc) from exc
        await self.datasources.datalink_status(datasource_id)
        return result

    async def resolve(
        self, datasource_id: str, payload: DataLinkResolveCandidateRequest
    ) -> DataLinkVersionPublishRead:
        source = await self.datasources.get_readable(datasource_id)
        self._require_schema(source.schema_revision, payload.schema_revision)
        try:
            result = await self.client.resolve_candidate(datasource_id, payload)
        except DataLinkClientError as exc:
            raise DataSourceService._to_app_error(exc) from exc
        await self.datasources.datalink_status(datasource_id)
        return result

    @staticmethod
    def _require_schema(current: int, requested: int) -> None:
        if current != requested:
            raise AppError(ErrorCode.HEAD_STALE, "Schema 版本已变化，请刷新后重试", status_code=409)
