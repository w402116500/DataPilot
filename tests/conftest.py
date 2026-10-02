from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from application.datalink_client import DataLinkClientError
from contracts.datalink import (
    DataLinkBuildRead,
    DataLinkBuildStatus,
    DataLinkErrorCode,
    DataLinkGraphRead,
    DataLinkHealthRead,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    DataLinkRemoveResult,
    DataLinkStatusRead,
)
from fastapi.testclient import TestClient
from server.app import create_app
from server.config import Settings


def sync_sqlite_url(async_url: str) -> str:
    return async_url.replace("+aiosqlite", "")


def run_migrations(database_url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_sqlite_url(database_url))
    command.upgrade(config, "head")


class FakeDataLinkClient:
    """测试专用的受控 DataLink 端口，避免数据源测试访问网络或真实模型。"""

    def __init__(self) -> None:
        self._builds: dict[str, DataLinkBuildRead] = {}

    async def health(self) -> DataLinkHealthRead:
        return DataLinkHealthRead(
            status="healthy",
            graph_database="ok",
            source_root="ok",
            model_configured=True,
            mcp_mounted=True,
        )

    async def rebuild(self, payload: DataLinkRebuildRequest) -> DataLinkRebuildResult:
        build = DataLinkBuildRead(
            build_id=f"build_{payload.datasource_id}_{payload.schema_revision}",
            datasource_id=payload.datasource_id,
            schema_revision=payload.schema_revision,
            attempt_no=1,
            status=DataLinkBuildStatus.COMPLETED,
            graph_version=f"graph_{payload.datasource_id}_{payload.schema_revision}",
            created_at=datetime.now(UTC),
        )
        self._builds[payload.datasource_id] = build
        return DataLinkRebuildResult(
            build_id=build.build_id,
            datasource_id=build.datasource_id,
            status=build.status,
            requested_schema_revision=build.schema_revision,
            graph_version=build.graph_version,
        )

    async def status(self, datasource_id: str) -> DataLinkStatusRead:
        build = self._builds.get(datasource_id)
        return DataLinkStatusRead(
            datasource_id=datasource_id,
            current_graph_version=build.graph_version if build else None,
            current_build=build,
        )

    async def graph(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
    ) -> DataLinkGraphRead:
        build = self._builds.get(datasource_id)
        return DataLinkGraphRead(
            datasource_id=datasource_id,
            graph_version=graph_version or (build.graph_version if build else None),
        )

    async def remove(self, datasource_id: str) -> DataLinkRemoveResult:
        return DataLinkRemoveResult(
            datasource_id=datasource_id,
            removed=self._builds.pop(datasource_id, None) is not None,
        )

    async def relation(self, datasource_id: str, relation_id: str, *, graph_version: str):
        raise DataLinkClientError(DataLinkErrorCode.INVALID_QUERY, "关系不存在", status_code=409)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        script_workspace_root=tmp_path / "script-workspaces",
        runtime_trace_root=tmp_path / "runtime-traces",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )


@pytest.fixture
def migrated_settings(settings: Settings) -> Settings:
    run_migrations(settings.metadata_database_url)
    return settings


@pytest.fixture
def client(migrated_settings: Settings) -> Iterator[TestClient]:
    app = create_app(migrated_settings, datalink_client=FakeDataLinkClient())
    with TestClient(app) as test_client:
        yield test_client
