"""集中装配 DataSourceService，保证 HTTP 与内部分析共用同一 Data Gateway 链路。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from contracts.sensitive_fields import SensitiveFieldPolicy
from data_gateway import AdapterRegistry, DataGateway
from metadata.repositories import ArtifactRepository, DataSourceRepository, SqlAuditRepository
from sqlalchemy.ext.asyncio import AsyncSession

from application.artifacts import ArtifactStore
from application.datalink_client import DataLinkClient, DataLinkManagementPort
from application.datasources import DataSourceService
from application.secret_cipher import SecretCipher

if TYPE_CHECKING:
    from runtime.run_lifecycle import RunLifecycleCoordinator
    from server.config import Settings


def build_datasource_service(
    db: AsyncSession,
    settings: Settings,
    *,
    datalink_client: DataLinkManagementPort | None = None,
    run_lifecycle: RunLifecycleCoordinator | None = None,
    cipher: SecretCipher | None = None,
) -> DataSourceService:
    """创建唯一的数据源服务组合，供 HTTP 和固定分析 Run 共同使用。"""

    artifact_store = ArtifactStore(ArtifactRepository(db), settings.artifact_root)
    gateway = DataGateway(
        registry=AdapterRegistry(),
        audits=SqlAuditRepository(db),
        artifacts=artifact_store,
        policy=SensitiveFieldPolicy(),
        default_query_limit=settings.default_query_limit,
        max_query_limit=settings.max_query_limit,
        query_timeout_seconds=settings.query_timeout_seconds,
    )
    return DataSourceService(
        DataSourceRepository(db),
        gateway,
        datalink_client
        or DataLinkClient(
            settings.datalink_base_url,
            timeout_seconds=settings.datalink_timeout_seconds,
        ),
        datasource_root=settings.datasource_root,
        max_upload_mb=settings.max_upload_mb,
        run_lifecycle=run_lifecycle,
        cipher=cipher or SecretCipher(settings.secret_master_key),
    )
