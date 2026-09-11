"""协调后台执行 Connect、Extract、Profile、Infer、Map、Store 的建图应用服务。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from contracts.datalink import (
    DataLinkBuildStatus,
    DataLinkErrorCode,
    DataLinkNodeType,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
)

from server.config import Settings
from server.connector import create_connector
from server.connector.base import ConnectorError
from server.connector.grants import ConnectionGrantResolver
from server.connector.mysql import MySqlConnector
from server.connector.paths import resolve_source_path
from server.extractor.tabular import TabularExtractor
from server.graph.repository import BuildClaim, GraphBuildRecord, GraphRepository
from server.inferrer.correlated import CorrelationInferrer
from server.inferrer.distribution import DistributionInferrer
from server.inferrer.joinable import JoinableInferrer
from server.inferrer.synonym import SynonymInferrer
from server.mapper.client import (
    ModelConfigurationError,
    ModelRequestTimeoutError,
    ModelResponseError,
    ModelResponseFormatError,
    ModelUpstreamError,
    SemanticModelClient,
)
from server.mapper.mapper import SemanticMapper, SemanticMappingValidationError
from server.models.graph import GraphEmbedding, GraphNode
from server.profiler.columns import ColumnProfiler
from server.revisions import RevisionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildExecutionError(RuntimeError):
    """Build 未成功时携带受限记录，供受控调用转为稳定错误响应。"""

    build: GraphBuildRecord


@dataclass(frozen=True)
class BuildAlreadyRunningError(RuntimeError):
    """重复请求命中活动 Build；它是受控状态，不是构建失败。"""

    build: GraphBuildRecord


@dataclass(frozen=True)
class BuildSubmission:
    """一次 REST rebuild 的持久化认领结果和是否需要启动后台执行。"""

    result: DataLinkRebuildResult
    claim: BuildClaim | None


class DataLinkBuildService:
    """协调版本化建图；耗时模型映射在独立后台执行。"""

    def __init__(
        self,
        settings: Settings,
        repository: GraphRepository,
        model_client: SemanticModelClient,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.model_client = model_client

    def rebuild(self, request: DataLinkRebuildRequest) -> DataLinkRebuildResult:
        """保留阻塞入口，供不经过 HTTP 调度的受控调用和测试使用。"""

        submission = self.submit_rebuild(request)
        if submission.claim is None:
            return submission.result
        return self._execute_created_claim(request, submission.claim)

    def submit_rebuild(self, request: DataLinkRebuildRequest) -> BuildSubmission:
        """认领一次 Build 并立即返回运行状态，不在 HTTP 请求中等待模型。"""

        claim = self.repository.claim_build(
            request.datasource_id,
            request.schema_revision,
            request.rebuild_key,
            connection_revision=request.connection_revision,
        )
        if claim.should_execute:
            return BuildSubmission(
                result=_running_rebuild_result(claim.build, request.schema_revision),
                claim=claim,
            )
        if claim.disposition == "blocked":
            raise BuildAlreadyRunningError(claim.build)
        if claim.build.status == DataLinkBuildStatus.RUNNING:
            # 同一 rebuild key 的网络重试复用同一个后台 Build，不重复入队。
            return BuildSubmission(
                result=_running_rebuild_result(claim.build, request.schema_revision),
                claim=None,
            )
        if claim.build.status == DataLinkBuildStatus.COMPLETED:
            return BuildSubmission(
                result=_rebuild_result(claim.build, request.schema_revision),
                claim=None,
            )
        raise BuildExecutionError(claim.build)

    def execute_submitted_rebuild(
        self,
        request: DataLinkRebuildRequest,
        claim: BuildClaim,
    ) -> None:
        """执行已认领的后台 Build；失败已持久化，不能让任务异常覆盖旧 Head。"""

        build = self.repository.get_build(claim.build.id)
        if build is None or build.status != DataLinkBuildStatus.RUNNING:
            return
        try:
            self._execute_created_claim(
                request,
                BuildClaim(build=build, disposition="created"),
            )
        except BuildExecutionError as exc:
            logger.info(
                "DataLink graph build finished as failed",
                extra={"build_id": exc.build.id, "error_code": str(exc.build.error_code)},
            )

    def _execute_created_claim(
        self, request: DataLinkRebuildRequest, claim: BuildClaim
    ) -> DataLinkRebuildResult:
        """运行新 Build；失败记录经过脱敏归类后保留旧 Head。"""

        build = claim.build
        connector = None
        deadline = monotonic() + self.settings.build_timeout_seconds
        try:
            if request.source_kind == "connection":
                grant = ConnectionGrantResolver(self.settings).consume(request, build.id)
                connector = MySqlConnector(grant, deadline)
            else:
                source_path = resolve_source_path(
                    self.settings.source_root, request.source_ref, request.source_type
                )
                connector = self._connector_for(request, source_path)
            datasource = connector.inspect()
            structure = TabularExtractor().extract(datasource)
            profiles = ColumnProfiler().profile_datasource(connector, datasource)
            joinable_edges = JoinableInferrer().infer(structure.nodes, profiles)
            explanation_edges = (
                *SynonymInferrer().infer(structure.nodes, profiles),
                *DistributionInferrer().infer(structure.nodes, profiles),
                *CorrelationInferrer().infer(structure.nodes, profiles, joinable_edges, connector),
            )
            semantic = SemanticMapper(self.model_client).map_columns(
                request.datasource_id, structure.columns, profiles
            )
            nodes = (*structure.nodes, *semantic.nodes)
            edges = (*structure.edges, *joinable_edges, *explanation_edges, *semantic.edges)
            embeddings = self._embeddings(nodes)
            if request.source_kind == "connection":
                connector.inspect()
                ConnectionGrantResolver(self.settings).consume(request, build.id)
            if monotonic() >= deadline:
                raise ConnectorError(
                    DataLinkErrorCode.BUILD_FAILED, "DataLink Build deadline exceeded"
                )
            completed = RevisionService(
                self.repository.storage, self.repository
            ).store_rebuilt_graph(
                build,
                nodes,
                edges,
                profiles,
                embeddings,
                structure.pending_edges,
            )
        except ConnectorError as exc:
            failed = self.repository.fail_build(build.id, exc.code, exc.message)
            raise BuildExecutionError(failed) from exc
        except ModelConfigurationError as exc:
            failed = self.repository.fail_build(
                build.id,
                DataLinkErrorCode.MODEL_CONFIG_INVALID,
                "DataLink model configuration is invalid",
            )
            raise BuildExecutionError(failed) from exc
        except ModelRequestTimeoutError as exc:
            failed = self.repository.fail_build(
                build.id,
                DataLinkErrorCode.MODEL_REQUEST_TIMEOUT,
                "DataLink semantic model request timed out",
            )
            raise BuildExecutionError(failed) from exc
        except ModelUpstreamError as exc:
            failed = self.repository.fail_build(
                build.id,
                DataLinkErrorCode.MODEL_UPSTREAM_ERROR,
                "DataLink semantic model service is unavailable",
            )
            raise BuildExecutionError(failed) from exc
        except ModelResponseFormatError as exc:
            failed = self.repository.fail_build(
                build.id,
                DataLinkErrorCode.MODEL_RESPONSE_INVALID,
                "DataLink semantic model response is invalid",
            )
            raise BuildExecutionError(failed) from exc
        except ModelResponseError as exc:
            failed = self.repository.fail_build(
                build.id,
                DataLinkErrorCode.MODEL_RESPONSE_ERROR,
                "DataLink semantic model response is unavailable",
            )
            raise BuildExecutionError(failed) from exc
        except SemanticMappingValidationError as exc:
            failed = self.repository.fail_build(
                build.id,
                DataLinkErrorCode.SEMANTIC_MAPPING_INVALID,
                "DataLink semantic mapping is invalid",
            )
            raise BuildExecutionError(failed) from exc
        except ValueError as exc:
            failed = self.repository.fail_build(
                build.id, DataLinkErrorCode.BUILD_FAILED, "Semantic graph build failed"
            )
            raise BuildExecutionError(failed) from exc
        except Exception as exc:
            logger.error(
                "DataLink graph build failed unexpectedly",
                extra={"build_id": build.id, "error_type": type(exc).__name__},
            )
            failed = self.repository.fail_build(
                build.id, DataLinkErrorCode.BUILD_FAILED, "Graph build failed"
            )
            raise BuildExecutionError(failed) from exc
        finally:
            if connector is not None:
                connector.close()
        return _rebuild_result(completed, request.schema_revision)

    def _connector_for(self, request: DataLinkRebuildRequest, source_path: Path):
        """通过 Connector 工厂选择唯一只读实现，避免服务层维护第二套路由。"""

        return create_connector(
            request.source_type,
            source_path,
            request.datasource_id,
            request.schema_revision,
        )

    def _embeddings(self, nodes: Sequence[GraphNode]) -> tuple[GraphEmbedding, ...]:
        """模型配置了 Embedding 时生成可选检索向量；失败不影响已验证的建图结果。"""

        model_name = self.settings.embedding_model
        searchable_nodes = tuple(
            node
            for node in nodes
            if node.type
            in {
                DataLinkNodeType.TABLE,
                DataLinkNodeType.COLUMN,
                DataLinkNodeType.CONCEPT,
                DataLinkNodeType.ENTITY,
            }
        )
        if not model_name or not searchable_nodes:
            return ()
        texts = tuple(_searchable_text(node) for node in searchable_nodes)
        try:
            vectors = self.model_client.embed(texts)
        except (ModelConfigurationError, ModelResponseError, ValueError, TypeError):
            return ()
        if vectors is None or len(vectors) != len(searchable_nodes):
            return ()
        return tuple(
            GraphEmbedding(
                node_id=node.id,
                embedding_model=model_name,
                vector=vector,
                searchable_text=text,
            )
            for node, text, vector in zip(searchable_nodes, texts, vectors, strict=True)
        )


def _rebuild_result(build: GraphBuildRecord, schema_revision: int) -> DataLinkRebuildResult:
    """仅把完成 Build 转成 rebuild 成功响应，失败 Build 走明确错误码。"""

    if build.status != DataLinkBuildStatus.COMPLETED:
        raise BuildExecutionError(build)
    return DataLinkRebuildResult(
        build_id=build.id,
        datasource_id=build.datasource_id,
        status=build.status,
        requested_schema_revision=schema_revision,
        connection_revision=build.connection_revision,
        graph_version=build.graph_version,
        publication_state=build.publication_state,
    )


def _running_rebuild_result(build: GraphBuildRecord, schema_revision: int) -> DataLinkRebuildResult:
    """运行中 Build 只暴露其状态，不把候选 graph version 当成可读版本。"""

    if build.status != DataLinkBuildStatus.RUNNING:
        raise ValueError("Only a running Build can be returned as pending")
    return DataLinkRebuildResult(
        build_id=build.id,
        datasource_id=build.datasource_id,
        status=build.status,
        requested_schema_revision=schema_revision,
        connection_revision=build.connection_revision,
        publication_state="candidate",
    )


def _searchable_text(node: GraphNode) -> str:
    """为可选向量检索构造节点名、描述与别名，不含字段画像中的真实取值。"""

    return "\n".join(part for part in (node.name, node.description, *node.aliases) if part)
