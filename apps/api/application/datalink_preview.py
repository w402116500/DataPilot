"""Published semantic preview through the same MCP port and projection as Runs."""

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    DataLinkExploreCommand,
    DataLinkExploreResponse,
)
from agent_runtime.datalink_semantics import (
    project_datalink_semantic_context,
    strip_sensitive_typical_values,
)
from agent_runtime.ports import CancellationSignal, DataLinkPort
from contracts.datalink import (
    DataLinkDraftPreviewRead,
    DataLinkDraftPreviewRequest,
    DataLinkPreviewRead,
    DataLinkPreviewRequest,
)
from contracts.datasources import DataSourceRead
from contracts.errors import AppError, ErrorCode
from contracts.sensitive_fields import SensitiveFieldPolicy
from contracts.status import DataSourceStatus

from application.datasources import DataSourceService


class DataLinkPreviewService:
    def __init__(self, datasources: DataSourceService, port: DataLinkPort) -> None:
        self.datasources = datasources
        self.port = port

    async def preview(
        self, datasource_id: str, payload: DataLinkPreviewRequest, cancellation: CancellationSignal
    ) -> DataLinkPreviewRead:
        source = await self.datasources.get(datasource_id)
        if source.status not in {DataSourceStatus.SCHEMA_READY, DataSourceStatus.READY}:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY, "Data source is not available", status_code=409
            )
        if source.datalink_graph_version is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY, "DataLink graph is not built", status_code=409
            )
        if source.datalink_graph_version != payload.graph_version:
            raise AppError(
                ErrorCode.GRAPH_VERSION_NOT_FOUND,
                "DataLink version changed; refresh before preview",
                status_code=409,
            )
        response = await self.port.explore(
            DataLinkExploreCommand(
                datasource_id=source.id,
                schema_revision=source.schema_revision,
                graph_version=payload.graph_version,
                query=payload.query,
                focus=payload.focus,
                max_nodes=payload.max_nodes,
            ),
            cancellation,
        )
        if isinstance(response, AgentFailure):
            unavailable = response.code == AgentErrorCode.DATALINK_UNAVAILABLE
            raise AppError(
                ErrorCode.DATALINK_UNAVAILABLE if unavailable else ErrorCode.INVALID_QUERY,
                response.message,
                status_code=503 if unavailable else 409,
            )
        # 预览忠实展示"模型可见投影"，因此同样先剥离敏感列典型取值。
        sanitized = strip_sensitive_typical_values(response, _sensitive_policy(source))
        return DataLinkPreviewRead(
            datasource_id=source.id,
            schema_revision=source.schema_revision,
            graph_version=payload.graph_version,
            query=payload.query,
            focus=payload.focus,
            max_nodes=payload.max_nodes,
            semantic_context=project_datalink_semantic_context(sanitized),
            retrieval_mode=response.result.retrieval_mode,
            is_truncated=response.result.is_truncated,
        )

    async def preview_draft(
        self, datasource_id: str, payload: DataLinkDraftPreviewRequest
    ) -> DataLinkDraftPreviewRead:
        result = await self.datasources.datalink_draft_preview(datasource_id, payload)
        source = await self.datasources.get(datasource_id)
        sanitized = strip_sensitive_typical_values(
            DataLinkExploreResponse(result=result.result, cache_hit=False),
            _sensitive_policy(source),
        )
        context = project_datalink_semantic_context(sanitized)
        return DataLinkDraftPreviewRead(
            datasource_id=result.datasource_id,
            base_graph_version=result.base_graph_version,
            schema_revision=result.schema_revision,
            draft_revision=result.draft_revision,
            semantic_context=context,
            is_truncated=result.result.is_truncated,
        )


def _sensitive_policy(source: DataSourceRead) -> SensitiveFieldPolicy:
    """按 DataSource 投影解析遮蔽清单；规则唯一所有者仍是共享契约。"""

    return SensitiveFieldPolicy(
        mask_fields=frozenset(source.mask_fields or ()),
        confirmed=source.mask_fields_confirmed,
    )
