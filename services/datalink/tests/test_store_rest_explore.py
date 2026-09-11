"""DataLink Store、REST 与唯一 MCP 探索入口的端到端边界测试。"""

from __future__ import annotations

import asyncio
import math
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from contracts.datalink import (
    DataLinkBuildStatus,
    DataLinkDraftSaveRequest,
    DataLinkEdgeEvidenceRead,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkExploreRequest,
    DataLinkNodeType,
    DataLinkPublishRequest,
    DataLinkRebuildRequest,
)
from contracts.sensitive_fields import SensitiveFieldPolicy
from contracts.status import DataSourceType
from fastapi.testclient import TestClient

from server.config import Settings
from server.graph.repository import BuildClaim, GraphBuildRecord, GraphRepository
from server.graph.storage import GraphStorage
from server.main import create_app
from server.mapper.client import (
    ModelRequestTimeoutError,
    ModelResponseError,
    ModelResponseFormatError,
    ModelUpstreamError,
    SemanticModelClient,
)
from server.mapper.mapper import SemanticMappingValidationError
from server.mapper.schemas import (
    ConceptMapping,
    EntityMapping,
    MappingColumnInput,
    SemanticMappingResponse,
    SemanticMergeResponse,
)
from server.mcp.server import create_mcp_server
from server.models.graph import GraphEdge, GraphEmbedding, GraphNode, GraphPendingEdge
from server.models.profile import ColumnProfile
from server.retrieval.explore import (
    _QUERY_EMBEDDING_FAILED_WARNING,
    GraphAccessError,
    GraphExplorer,
    _direct_entity_ids,
    _ScoredNode,
)


class DemoSemanticClient(SemanticModelClient):
    """固定、脱网的模型替身，只为验证正式流水线的契约拼接。"""

    def map_batch(self, columns: Sequence[MappingColumnInput]) -> SemanticMappingResponse:
        """为本批每列生成一个独立 Concept，确保完整性校验由真实代码执行。"""

        concepts = [
            ConceptMapping(
                name=f"{column.table_name}_{column.column_name}",
                description=f"Business meaning for {column.column_name}",
                aliases=[],
                columns=[column.column_id],
                confidence=0.8,
            )
            for column in columns
        ]
        return SemanticMappingResponse(
            concepts=concepts,
            entities=[
                EntityMapping(
                    name="demo_entity",
                    description="Demo entity",
                    aliases=[],
                    concept_names=[concept.name for concept in concepts],
                    confidence=0.8,
                )
            ],
        )

    def judge_merges(self, new_nodes, existing_nodes, candidates) -> SemanticMergeResponse:
        """Demo 不合并任何节点，隔离 Store/REST 验证与模型语义质量。"""

        return SemanticMergeResponse(merges=[])

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        """默认不配置向量，验证关键词检索仍完整可用。"""

        return None


class FailingMappingClient(DemoSemanticClient):
    """模拟 Map 本身失败，验证 Build 不会发布缺失语义的新图。"""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def map_batch(self, columns: Sequence[MappingColumnInput]) -> SemanticMappingResponse:
        raise self.error


class EmbeddingSemanticClient(DemoSemanticClient):
    """记录建图索引文本，验证物理节点也参与向量检索。"""

    def __init__(self) -> None:
        self.embedding_texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        self.embedding_texts.extend(texts)
        return tuple((float(index + 1), 1.0) for index, _ in enumerate(texts))


class QueryEmbeddingClient(DemoSemanticClient):
    """为向量检索回归固定查询向量，不依赖真实模型或语言环境。"""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        return tuple((1.0, 0.0) for _ in texts)


class FailingQueryEmbeddingClient(DemoSemanticClient):
    """已配置 embedding 客户端，但 query 向量返回空。"""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        return None


def make_repository(tmp_path: Path) -> GraphRepository:
    """构造隔离的已初始化图谱库。"""

    repository = GraphRepository(GraphStorage(tmp_path / "datalink.db"))
    repository.initialize()
    return repository


def seed_completed_graph(repository: GraphRepository):
    """写入一张最小但包含外键、语义边和敏感画像的完成图谱。"""

    build = repository.claim_build("ds_demo", 1).build
    nodes = (
        _table("orders"),
        _column("orders", "customer_id"),
        _column("orders", "email"),
        _table("customers"),
        _column("customers", "customer_id"),
        GraphNode(
            id="concept:ds_demo:customer_identifier",
            type=DataLinkNodeType.CONCEPT,
            name="customer identifier",
            description="Customer identifier used by orders",
        ),
    )
    edges = (
        _contains("orders", "customer_id"),
        _contains("orders", "email"),
        _contains("customers", "customer_id"),
        GraphEdge(
            id="edge:foreign-key:orders:customer",
            source_id="column:ds_demo:orders:customer_id",
            target_id="column:ds_demo:customers:customer_id",
            type=DataLinkEdgeType.FOREIGN_KEY,
            confidence=1.0,
            evidence=DataLinkEdgeEvidenceRead(
                kind="explicit_foreign_key",
                summary="orders.customer_id references customers.customer_id",
            ),
        ),
        GraphEdge(
            id="edge:represents:orders:customer",
            source_id="column:ds_demo:orders:customer_id",
            target_id="concept:ds_demo:customer_identifier",
            type=DataLinkEdgeType.REPRESENTS,
            confidence=0.9,
        ),
    )
    profiles = (
        _profile("orders", "customer_id"),
        _profile("orders", "email", top_values=("private@example.test",)),
        _profile("customers", "customer_id"),
    )
    return repository.store_completed_graph(build.id, nodes, edges, profiles)


def test_store_rolls_back_when_an_edge_endpoint_is_not_in_this_build(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_demo", 1).build
    nodes = (_table("orders"),)
    invalid_edge = GraphEdge(
        id="edge:missing",
        source_id=nodes[0].id,
        target_id="column:ds_demo:missing:id",
        type=DataLinkEdgeType.JOINABLE,
        confidence=0.5,
    )

    with pytest.raises(ValueError, match="endpoint"):
        repository.store_completed_graph(build.id, nodes, (invalid_edge,), ())

    assert repository.get_build(build.id).status.value == "running"
    with repository.storage.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0


def test_pending_edge_is_versioned_but_cannot_become_a_join_path(tmp_path: Path) -> None:
    """缺失目标的外键候选会保存下来，但不会变成正式图边或 Join 证据。"""

    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_demo", 1).build
    nodes = (_table("orders"), _column("orders", "customer_id"))
    completed = repository.store_completed_graph(
        build.id,
        nodes,
        (_contains("orders", "customer_id"),),
        (),
        pending_edges=(
            GraphPendingEdge(
                id="pending:orders:customer",
                source_id="column:ds_demo:orders:customer_id",
                target_ref="column:ds_demo:customers:customer_id",
                type=DataLinkEdgeType.FOREIGN_KEY,
                confidence=1.0,
                missing_endpoints=("target",),
            ),
        ),
    )
    explorer = GraphExplorer(repository)

    result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_demo",
            graph_version=completed.graph_version,
            query="orders",
            focus="join_paths",
        )
    )

    with repository.storage.connection() as connection:
        pending_count = connection.execute("SELECT COUNT(*) FROM pending_edges").fetchone()[0]
    assert pending_count == 1
    assert result.join_paths == []


def test_explore_is_version_isolated_masks_sensitive_values_and_uses_only_join_edges(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    completed = seed_completed_graph(repository)
    explorer = GraphExplorer(
        repository,
        sensitive_policy=SensitiveFieldPolicy(
            mask_fields=frozenset({"email"}),
            confirmed=True,
        ),
    )

    profile_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_demo",
            graph_version=completed.graph_version,
            query="email",
            focus="data_profile",
            max_nodes=4,
        )
    )
    join_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_demo",
            graph_version=completed.graph_version,
            query="orders customers",
            focus="join_paths",
            max_nodes=4,
        )
    )

    email_node = next(node for node in profile_result.nodes if node.name == "email")
    assert email_node.profile is not None
    assert email_node.profile.top_values == []
    assert "private@example.test" not in profile_result.model_dump_json()
    assert join_result.join_paths[0].steps[0].edge_type == DataLinkEdgeType.FOREIGN_KEY
    assert all(edge.type != DataLinkEdgeType.REPRESENTS for edge in join_result.edges)

    with pytest.raises(GraphAccessError) as error:
        explorer.explore(
            DataLinkExploreRequest(
                datasource_id="ds_other",
                graph_version=completed.graph_version,
                query="orders",
            )
        )
    assert error.value.code.value == "DATASOURCE_MISMATCH"


def test_read_graph_uses_browser_projection_without_profile_values(tmp_path: Path) -> None:
    """旧整图接口也不能绕过数据地图的样例值边界。"""

    repository = make_repository(tmp_path)
    completed = seed_completed_graph(repository)

    result = GraphExplorer(repository).read_graph("ds_demo", completed.graph_version)

    assert result.model_dump().get("profiles") is None
    email = next(node for node in result.nodes if node.name == "email")
    assert email.profile is not None
    assert email.profile.dtype == "text"
    payload = result.model_dump_json()
    assert "private@example.test" not in payload
    assert "top_values" not in payload
    assert "sample_values" not in payload


def test_explore_returns_semantic_context_for_chinese_columns_and_keeps_join_paths_physical(
    tmp_path: Path,
) -> None:
    """Schema 检索保留字段语义，Join 检索则只返回可连接的物理节点。"""

    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_loans", 1).build
    loan_columns = (
        _column("loans", "account_id", datasource_id="ds_loans"),
        _column("loans", "purpose", datasource_id="ds_loans"),
        _column("loans", "int.rate", datasource_id="ds_loans"),
        _column("loans", "not.fully.paid", datasource_id="ds_loans"),
    )
    account_column = _column("accounts", "account_id", datasource_id="ds_loans")
    concepts = (
        _concept("loan_purpose", "贷款用途", loan_columns[1].id),
        _concept("interest_rate", "平均利率", loan_columns[2].id),
        _concept("not_fully_paid", "未足额还款", loan_columns[3].id),
        _concept("borrower_account", "贷款账户", account_column.id),
    )
    entity = GraphNode(
        id="entity:ds_loans:loan",
        type=DataLinkNodeType.ENTITY,
        name="loan",
        description="贷款记录",
    )
    nodes = (
        _table("loans", datasource_id="ds_loans"),
        _table("accounts", datasource_id="ds_loans"),
        *loan_columns,
        account_column,
        *concepts,
        entity,
    )
    edges = (
        *(_contains("loans", column.name, datasource_id="ds_loans") for column in loan_columns),
        _contains("accounts", "account_id", datasource_id="ds_loans"),
        *(
            _represents(column_id, concept.id)
            for concept, column_id in zip(
                concepts,
                (
                    loan_columns[1].id,
                    loan_columns[2].id,
                    loan_columns[3].id,
                    account_column.id,
                ),
                strict=True,
            )
        ),
        *(_has_concept(entity.id, concept.id) for concept in concepts),
        GraphEdge(
            id="edge:foreign-key:loans:accounts",
            source_id=loan_columns[0].id,
            target_id=account_column.id,
            type=DataLinkEdgeType.FOREIGN_KEY,
            confidence=1.0,
        ),
    )
    completed = repository.store_completed_graph(build.id, nodes, edges, ())

    explorer = GraphExplorer(repository)
    default_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_loans",
            graph_version=completed.graph_version,
            query="贷款用途",
            max_nodes=12,
        )
    )
    entity_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_loans",
            graph_version=completed.graph_version,
            query="loan",
            focus="schema",
            max_nodes=12,
        )
    )
    join_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_loans",
            graph_version=completed.graph_version,
            query="贷款用途 平均利率 未足额还款 贷款账户",
            focus="join_paths",
            max_nodes=6,
        )
    )

    assert {node.name for node in default_result.nodes} >= {
        "loans",
        "purpose",
        "loan_purpose",
        "loan",
    }
    assert {edge.type for edge in default_result.edges} >= {
        DataLinkEdgeType.REPRESENTS,
        DataLinkEdgeType.HAS_CONCEPT,
    }
    assert any(
        edge.source == loan_columns[1].id
        and edge.target == concepts[0].id
        and edge.type == DataLinkEdgeType.REPRESENTS
        for edge in default_result.edges
    )
    assert {node.name for node in entity_result.nodes} >= {
        "loans",
        "purpose",
        "int.rate",
        "not.fully.paid",
        "loan",
        "loan_purpose",
    }
    assert any(
        edge.source == entity.id
        and edge.target == concepts[0].id
        and edge.type == DataLinkEdgeType.HAS_CONCEPT
        for edge in entity_result.edges
    )
    assert any(
        edge.source == loan_columns[1].id
        and edge.target == concepts[0].id
        and edge.type == DataLinkEdgeType.REPRESENTS
        for edge in entity_result.edges
    )
    entity_concept_ids = {
        edge.target
        for edge in entity_result.edges
        if edge.source == entity.id and edge.type == DataLinkEdgeType.HAS_CONCEPT
    }
    entity_result_column_ids = {
        node.id for node in entity_result.nodes if node.type == DataLinkNodeType.COLUMN
    }
    assert any(
        edge.source in entity_result_column_ids
        and edge.target in entity_concept_ids
        and edge.type == DataLinkEdgeType.REPRESENTS
        for edge in entity_result.edges
    )
    assert {node.name for node in join_result.nodes} >= {
        "purpose",
        "int.rate",
        "not.fully.paid",
    }
    assert {node.type for node in join_result.nodes} <= {
        DataLinkNodeType.COLUMN,
        DataLinkNodeType.TABLE,
    }
    assert {node.name for node in join_result.nodes if node.type == DataLinkNodeType.TABLE} >= {
        "loans",
        "accounts",
    }
    assert join_result.join_paths
    assert all(
        step.edge_type in {DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE}
        for path in join_result.join_paths
        for step in path.steps
    )


def test_schema_focus_keeps_a_complete_entity_concept_column_chain_under_node_budget(
    tmp_path: Path,
) -> None:
    """精确 Entity 查询必须保留可投影到字段的语义链，而不只返回孤立语义节点。"""

    datasource_id = "ds_entity"
    repository = make_repository(tmp_path)
    build = repository.claim_build(datasource_id, 1).build
    table = _table("records", datasource_id=datasource_id)
    columns = tuple(
        _column("records", chr(ord("a") + index), datasource_id=datasource_id)
        for index in range(10)
    )
    concepts_by_column = {
        column.id: GraphNode(
            id=f"concept:{datasource_id}:concept_{chr(ord('j') - index)}",
            type=DataLinkNodeType.CONCEPT,
            name=f"concept_{chr(ord('j') - index)}",
            description="Mapped semantic concept",
        )
        for index, column in enumerate(columns)
    }
    entity = GraphNode(
        id=f"entity:{datasource_id}:record_bundle",
        type=DataLinkNodeType.ENTITY,
        name="record_bundle",
        description="Grouping for the supplied records",
    )
    edges = (
        *(_contains("records", column.name, datasource_id=datasource_id) for column in columns),
        *(_represents(column.id, concepts_by_column[column.id].id) for column in columns),
        *(_has_concept(entity.id, concept.id) for concept in concepts_by_column.values()),
    )
    completed = repository.store_completed_graph(
        build.id,
        (table, *columns, *concepts_by_column.values(), entity),
        edges,
        (),
    )

    result = GraphExplorer(repository).explore(
        DataLinkExploreRequest(
            datasource_id=datasource_id,
            graph_version=completed.graph_version,
            query="record_bundle",
            focus="schema",
            max_nodes=12,
        )
    )

    column = columns[0]
    concept = concepts_by_column[column.id]
    node_ids = {node.id for node in result.nodes}
    assert {entity.id, concept.id, column.id} <= node_ids
    assert any(
        edge.source == entity.id
        and edge.target == concept.id
        and edge.type == DataLinkEdgeType.HAS_CONCEPT
        for edge in result.edges
    )
    assert any(
        edge.source == column.id
        and edge.target == concept.id
        and edge.type == DataLinkEdgeType.REPRESENTS
        for edge in result.edges
    )


def test_vector_semantic_matches_reserve_columns_before_direct_vector_noise(tmp_path: Path) -> None:
    """英文语义图也应通过向量概念命中保住中文问题对应的真实字段。"""

    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_loans", 1).build
    target_columns = (
        _column("dataset", "purpose", datasource_id="ds_loans"),
        _column("dataset", "int.rate", datasource_id="ds_loans"),
        _column("dataset", "not.fully.paid", datasource_id="ds_loans"),
    )
    noisy_columns = tuple(
        _column("dataset", f"noise_{index}", datasource_id="ds_loans") for index in range(6)
    )
    concepts = (
        _concept("loan_purpose", "Reason a loan was issued", target_columns[0].id),
        _concept("interest_rate", "Annual rate charged on a loan", target_columns[1].id),
        _concept("repayment_status", "Whether a loan was fully repaid", target_columns[2].id),
    )
    nodes = (
        _table("dataset", datasource_id="ds_loans"),
        *target_columns,
        *noisy_columns,
        *concepts,
    )
    edges = (
        *(
            _contains("dataset", column.name, datasource_id="ds_loans")
            for column in (*target_columns, *noisy_columns)
        ),
        *(
            _represents(column.id, concept.id)
            for column, concept in zip(target_columns, concepts, strict=True)
        ),
    )
    embeddings = (
        *(
            _embedding(column.id, score)
            for column, score in zip(
                target_columns,
                (0.55, 0.98, 0.97),
                strict=True,
            )
        ),
        *(
            _embedding(column.id, score)
            for column, score in zip(
                noisy_columns,
                (0.96, 0.95, 0.94, 0.93, 0.92, 0.91),
                strict=True,
            )
        ),
        *(
            _embedding(concept.id, score)
            for concept, score in zip(concepts, (0.99, 0.98, 0.97), strict=True)
        ),
    )
    completed = repository.store_completed_graph(build.id, nodes, edges, (), embeddings)

    result = GraphExplorer(repository, QueryEmbeddingClient()).explore(
        DataLinkExploreRequest(
            datasource_id="ds_loans",
            graph_version=completed.graph_version,
            query="贷款用途 平均利率 未足额还款",
            max_nodes=12,
        )
    )

    assert {node.name for node in result.nodes} >= {column.name for column in target_columns}
    assert {node.name for node in result.nodes} >= {concept.name for concept in concepts}
    assert len(result.nodes) == 12


def _embedding(node_id: str, similarity: float) -> GraphEmbedding:
    """构造与固定查询向量的二维余弦相似度。"""

    return GraphEmbedding(
        node_id=node_id,
        embedding_model="test-embedding",
        vector=(similarity, math.sqrt(1 - similarity**2)),
        searchable_text="test",
    )


def _seed_customer_channel_graph(
    repository: GraphRepository,
    *,
    datasource_id: str = "ds_customers",
    embeddings: Sequence[GraphEmbedding] = (),
    entity_name: str = "客户",
    entity_aliases: tuple[str, ...] = (),
    concept_aliases: tuple[str, ...] = (),
) -> tuple[GraphBuildRecord, GraphNode, GraphNode, GraphNode]:
    """最小图：实体「客户」、概念「获取渠道」、字段 channel。"""

    build = repository.claim_build(datasource_id, 1).build
    table = _table("customers", datasource_id=datasource_id)
    column = _column("customers", "channel", datasource_id=datasource_id)
    concept = GraphNode(
        id=f"concept:{datasource_id}:acquisition_channel",
        type=DataLinkNodeType.CONCEPT,
        name="获取渠道",
        aliases=concept_aliases,
    )
    entity = GraphNode(
        id=f"entity:{datasource_id}:customer",
        type=DataLinkNodeType.ENTITY,
        name=entity_name,
        aliases=entity_aliases,
    )
    completed = repository.store_completed_graph(
        build.id,
        (table, column, concept, entity),
        (
            _contains("customers", "channel", datasource_id=datasource_id),
            _represents(column.id, concept.id),
            _has_concept(entity.id, concept.id),
        ),
        (),
        embeddings,
    )
    return completed, entity, concept, column


def test_keyword_recall_hits_entity_name_in_chinese_question(tmp_path: Path) -> None:
    """无空格中文问句靠实体名命中，并展开到概念和字段。"""

    repository = make_repository(tmp_path)
    completed, entity, concept, column = _seed_customer_channel_graph(repository)

    result = GraphExplorer(repository).explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query="各获客渠道有多少客户？",
            max_nodes=12,
        )
    )

    names = {node.name for node in result.nodes}
    assert {entity.name, concept.name, column.name} <= names
    assert result.warnings == []


def test_short_name_and_ascii_column_queries_still_hit(tmp_path: Path) -> None:
    """短中文名和 ASCII 列名仍按完整出现命中。"""

    repository = make_repository(tmp_path)
    completed, _entity, concept, column = _seed_customer_channel_graph(repository)
    explorer = GraphExplorer(repository)

    concept_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query="获取渠道",
            max_nodes=12,
        )
    )
    column_result = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query="channel",
            max_nodes=12,
        )
    )

    assert {concept.name, column.name} <= {node.name for node in concept_result.nodes}
    assert column.name in {node.name for node in column_result.nodes}


def test_query_without_names_or_aliases_stays_empty(tmp_path: Path) -> None:
    """问句不含名称或别名时保持空结果和既有 warning。"""

    repository = make_repository(tmp_path)
    completed, _entity, _concept, _column = _seed_customer_channel_graph(repository)

    result = GraphExplorer(repository).explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query="今天天气如何",
            max_nodes=12,
        )
    )

    assert result.nodes == []
    assert result.warnings == ["No matching nodes were found in this graph version"]


def test_query_embedding_failure_warns_even_when_keyword_hits(tmp_path: Path) -> None:
    """有库存向量但 query embedding 返回空时回退关键词，且 warning 在命中后仍保留。"""

    repository = make_repository(tmp_path)
    column = _column("customers", "channel", datasource_id="ds_customers")
    completed, entity, _concept, _stored_column = _seed_customer_channel_graph(
        repository,
        embeddings=(_embedding(column.id, 0.9),),
    )
    explorer = GraphExplorer(repository, FailingQueryEmbeddingClient())

    hit = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query=entity.name,
            max_nodes=12,
        )
    )
    miss = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query="今天天气如何",
            max_nodes=12,
        )
    )

    assert hit.retrieval_mode == "keyword"
    assert {entity.name, column.name} <= {node.name for node in hit.nodes}
    assert _QUERY_EMBEDDING_FAILED_WARNING in hit.warnings
    assert miss.retrieval_mode == "keyword"
    assert miss.nodes == []
    assert "No matching nodes were found in this graph version" in miss.warnings
    assert _QUERY_EMBEDDING_FAILED_WARNING in miss.warnings


def test_ascii_name_does_not_substring_match_query_token(tmp_path: Path) -> None:
    """ASCII 名称必须等于问句里的 \\w+ token，border 不能命中 order。"""

    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_ascii", 1).build
    table = _table("order", datasource_id="ds_ascii")
    completed = repository.store_completed_graph(build.id, (table,), (), ())
    explorer = GraphExplorer(repository)

    miss = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_ascii",
            graph_version=completed.graph_version,
            query="border",
            max_nodes=12,
        )
    )
    hit = explorer.explore(
        DataLinkExploreRequest(
            datasource_id="ds_ascii",
            graph_version=completed.graph_version,
            query="order",
            max_nodes=12,
        )
    )

    assert miss.nodes == []
    assert "order" in {node.name for node in hit.nodes}


def test_alias_in_query_recalls_node_without_becoming_entity_anchor() -> None:
    """别名可以让节点得分；直接实体锚点只看实体名。"""

    entity = GraphNode(
        id="entity:ds_alias:customer",
        type=DataLinkNodeType.ENTITY,
        name="用户",
        aliases=("客户",),
    )
    scored = (_ScoredNode(node=entity, score=2.0),)

    assert _direct_entity_ids("客户", scored) == set()
    assert _direct_entity_ids("用户", scored) == {entity.id}


def test_alias_in_chinese_question_recalls_concept_and_entity(tmp_path: Path) -> None:
    """问句完整出现别名时召回对应节点并展开到字段。"""

    repository = make_repository(tmp_path)
    completed, entity, concept, column = _seed_customer_channel_graph(
        repository,
        datasource_id="ds_alias",
        entity_name="用户",
        entity_aliases=("客户",),
        concept_aliases=("获客渠道",),
    )

    result = GraphExplorer(repository).explore(
        DataLinkExploreRequest(
            datasource_id="ds_alias",
            graph_version=completed.graph_version,
            query="各获客渠道有多少客户？",
            max_nodes=12,
        )
    )

    assert {entity.name, concept.name, column.name} <= {node.name for node in result.nodes}


def test_stored_embeddings_without_client_stay_keyword_without_warning(tmp_path: Path) -> None:
    """未配置 embedding 客户端时即使有库存向量也走关键词，不新增 warning。"""

    repository = make_repository(tmp_path)
    column = _column("customers", "channel", datasource_id="ds_customers")
    completed, entity, _concept, _stored_column = _seed_customer_channel_graph(
        repository,
        embeddings=(_embedding(column.id, 0.9),),
    )

    result = GraphExplorer(repository).explore(
        DataLinkExploreRequest(
            datasource_id="ds_customers",
            graph_version=completed.graph_version,
            query=entity.name,
            max_nodes=12,
        )
    )

    assert result.retrieval_mode == "keyword"
    assert entity.name in {node.name for node in result.nodes}
    assert result.warnings == []


def test_browser_entries_and_subgraph_are_version_isolated_and_filter_relations(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    completed = seed_completed_graph(repository)
    explorer = GraphExplorer(repository)

    entries = explorer.list_browser_entries(
        "ds_demo",
        completed.graph_version,
        entry_type="table",
        query="order",
        page=1,
        page_size=1,
    )
    subgraph = explorer.read_browser_subgraph(
        "ds_demo",
        completed.graph_version,
        root_node_id="table:ds_demo:orders",
        edge_types=(DataLinkEdgeType.CONTAINS, DataLinkEdgeType.FOREIGN_KEY),
        hops=2,
    )

    assert entries.total == 1
    assert [entry.name for entry in entries.items] == ["orders"]
    assert subgraph.root_node_id == "table:ds_demo:orders"
    assert subgraph.total_node_count == 6
    assert subgraph.total_edge_count == 5
    assert {edge.type for edge in subgraph.edges} == {
        DataLinkEdgeType.CONTAINS,
        DataLinkEdgeType.FOREIGN_KEY,
    }
    assert subgraph.is_truncated is False
    assert subgraph.warnings == []
    browser_payload = subgraph.model_dump_json()
    assert "sample_values" not in browser_payload
    assert "top_values" not in browser_payload
    assert "min_value" not in browser_payload
    assert "max_value" not in browser_payload
    customer_id = next(node for node in subgraph.nodes if node.name == "customer_id")
    assert customer_id.profile is not None
    assert customer_id.profile.dtype == "text"

    foreign_keys_only = explorer.read_browser_subgraph(
        "ds_demo",
        completed.graph_version,
        root_node_id="table:ds_demo:orders",
        edge_types=(DataLinkEdgeType.FOREIGN_KEY,),
        hops=2,
    )
    assert {edge.type for edge in foreign_keys_only.edges} == {DataLinkEdgeType.FOREIGN_KEY}
    assert {
        "table:ds_demo:orders",
        "column:ds_demo:orders:customer_id",
        "column:ds_demo:customers:customer_id",
    }.issubset({node.id for node in foreign_keys_only.nodes})

    with pytest.raises(GraphAccessError) as missing_root:
        explorer.read_browser_subgraph(
            "ds_demo",
            completed.graph_version,
            root_node_id="table:ds_demo:missing",
        )
    assert missing_root.value.code.value == "INVALID_QUERY"

    with pytest.raises(GraphAccessError) as mismatched_version:
        explorer.read_browser_subgraph(
            "ds_other",
            completed.graph_version,
            root_node_id="table:ds_demo:orders",
        )
    assert mismatched_version.value.code.value == "DATASOURCE_MISMATCH"


def test_browser_subgraph_reports_node_limit_instead_of_claiming_to_be_complete(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_large", 1).build
    root = GraphNode(
        id="table:ds_large:orders",
        type=DataLinkNodeType.TABLE,
        name="orders",
    )
    columns = tuple(
        GraphNode(
            id=f"column:ds_large:orders:column_{index}",
            type=DataLinkNodeType.COLUMN,
            name=f"column_{index}",
            table_name="orders",
        )
        for index in range(81)
    )
    edges = tuple(
        GraphEdge(
            id=f"edge:contains:orders:column_{index}",
            source_id=root.id,
            target_id=column.id,
            type=DataLinkEdgeType.CONTAINS,
            confidence=1.0,
        )
        for index, column in enumerate(columns)
    )
    completed = repository.store_completed_graph(build.id, (root, *columns), edges, ())

    result = GraphExplorer(repository).read_browser_subgraph(
        "ds_large",
        completed.graph_version,
        root_node_id=root.id,
    )

    assert result.total_node_count == 82
    assert result.total_edge_count == 81
    assert len(result.nodes) == 80
    assert result.is_truncated is True
    assert result.warnings


def test_rest_graph_browser_endpoints_accept_repeated_edge_types_and_reject_bad_roots(
    tmp_path: Path,
) -> None:
    settings = Settings(
        source_root=tmp_path / "datasources",
        database_path=tmp_path / "datalink" / "datalink.db",
    )
    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        completed = seed_completed_graph(client.app.state.graph_repository)
        entries = client.get(
            "/v1/graphs/ds_demo/entries",
            params={
                "graph_version": completed.graph_version,
                "entry_type": "table",
                "query": "order",
                "page": 1,
                "page_size": 10,
            },
        )
        subgraph = client.get(
            "/v1/graphs/ds_demo/subgraph",
            params=[
                ("graph_version", completed.graph_version),
                ("root_node_id", "table:ds_demo:orders"),
                ("edge_types", DataLinkEdgeType.CONTAINS.value),
                ("edge_types", DataLinkEdgeType.FOREIGN_KEY.value),
                ("hops", "2"),
            ],
        )
        missing_root = client.get(
            "/v1/graphs/ds_demo/subgraph",
            params={
                "graph_version": completed.graph_version,
                "root_node_id": "table:ds_demo:missing",
            },
        )
        mismatch = client.get(
            "/v1/graphs/ds_other/entries",
            params={"graph_version": completed.graph_version},
        )

    assert entries.status_code == 200
    assert entries.json()["items"] == [
        {
            "id": "table:ds_demo:orders",
            "type": "table",
            "name": "orders",
            "description": None,
            "aliases": [],
        }
    ]
    assert subgraph.status_code == 200
    assert {edge["type"] for edge in subgraph.json()["edges"]} == {
        DataLinkEdgeType.CONTAINS.value,
        DataLinkEdgeType.FOREIGN_KEY.value,
    }
    assert missing_root.status_code == 409
    assert missing_root.json()["error"]["code"] == "INVALID_QUERY"
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "DATASOURCE_MISMATCH"


def test_rest_rebuild_reads_fixed_demo_and_exposes_only_safe_versioned_data(tmp_path: Path) -> None:
    source_root = tmp_path / "datasources"
    source_dir = source_root / "demo"
    source_dir.mkdir(parents=True)
    demo_database = Path(__file__).parents[3] / "data" / "demo" / "ecommerce.sqlite"
    shutil.copy2(demo_database, source_dir / "ecommerce.sqlite")
    settings = Settings(
        source_root=source_root,
        database_path=tmp_path / "datalink" / "datalink.db",
    )

    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        first_payload = {
            "datasource_id": "ds_demo",
            "rebuild_key": "rebuild_1",
            "source_type": DataSourceType.SQLITE.value,
            "source_ref": "demo/ecommerce.sqlite",
            "schema_revision": 1,
        }
        rebuild = client.post(
            "/v1/graphs/rebuild",
            json=first_payload,
        )
        assert rebuild.status_code == 202
        result = rebuild.json()
        assert result["status"] == "running"
        assert result["graph_version"] is None
        retry = client.post("/v1/graphs/rebuild", json=first_payload)
        assert retry.status_code == 202
        rebuilt = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "ds_demo",
                "rebuild_key": "rebuild_2",
                "source_type": DataSourceType.SQLITE.value,
                "source_ref": "demo/ecommerce.sqlite",
                "schema_revision": 1,
            },
        )
        assert rebuilt.status_code == 202
        rebuilt_result = rebuilt.json()

        graph = client.get("/v1/graphs/ds_demo")
        status = client.get("/v1/graphs/ds_demo/status")
        mismatch = client.get("/v1/graphs/another")
        first_remove = client.post("/v1/graphs/ds_demo/remove")
        second_remove = client.post("/v1/graphs/ds_demo/remove")

    assert graph.status_code == 200
    assert graph.json()["graph_version"] is not None
    assert all("embedding" not in node for node in graph.json()["nodes"])
    assert str(settings.database_path) not in graph.text
    assert retry.json()["build_id"] == result["build_id"]
    assert retry.json()["status"] == "completed"
    assert rebuilt_result["build_id"] != result["build_id"]
    assert rebuilt_result["graph_version"] is None
    assert status.json()["current_graph_version"] == graph.json()["graph_version"]
    assert mismatch.status_code == 404
    assert mismatch.json()["error"]["code"] == "GRAPH_VERSION_NOT_FOUND"
    assert first_remove.json()["removed"] is True
    assert second_remove.json()["removed"] is False


@pytest.mark.parametrize(
    ("mapping_error", "expected_error_code"),
    [
        (
            ModelRequestTimeoutError("model request timed out"),
            DataLinkErrorCode.MODEL_REQUEST_TIMEOUT,
        ),
        (
            ModelUpstreamError("upstream service failed"),
            DataLinkErrorCode.MODEL_UPSTREAM_ERROR,
        ),
        (
            ModelResponseError("mapping response is unavailable"),
            DataLinkErrorCode.MODEL_RESPONSE_ERROR,
        ),
        (
            ModelResponseFormatError("mapping response is invalid"),
            DataLinkErrorCode.MODEL_RESPONSE_INVALID,
        ),
        (
            SemanticMappingValidationError("concept points outside the current mapping batch"),
            DataLinkErrorCode.SEMANTIC_MAPPING_INVALID,
        ),
    ],
)
def test_rest_rebuild_keeps_previous_head_when_semantic_mapping_fails(
    tmp_path: Path,
    mapping_error: Exception,
    expected_error_code: DataLinkErrorCode,
) -> None:
    """Map 失败只能结束候选 Build，不能把结构空图切成当前版本。"""

    source_root = tmp_path / "datasources"
    source_dir = source_root / "demo"
    source_dir.mkdir(parents=True)
    demo_database = Path(__file__).parents[3] / "data" / "demo" / "ecommerce.sqlite"
    shutil.copy2(demo_database, source_dir / "ecommerce.sqlite")
    settings = Settings(
        source_root=source_root,
        database_path=tmp_path / "datalink" / "datalink.db",
    )

    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        first = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "ds_demo",
                "rebuild_key": "rebuild_success",
                "source_type": DataSourceType.SQLITE.value,
                "source_ref": "demo/ecommerce.sqlite",
                "schema_revision": 1,
            },
        )
        assert first.status_code == 202
        first_result = first.json()
        first_status = client.get("/v1/graphs/ds_demo/status")
        assert first_status.status_code == 200
        first_graph_version = first_status.json()["current_graph_version"]
        assert first_graph_version is not None
        client.app.state.build_service.model_client = FailingMappingClient(mapping_error)

        failed = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "ds_demo",
                "rebuild_key": "rebuild_fails_mapping",
                "source_type": DataSourceType.SQLITE.value,
                "source_ref": "demo/ecommerce.sqlite",
                "schema_revision": 1,
            },
        )
        status = client.get("/v1/graphs/ds_demo/status")
        previous_graph = client.get(f"/v1/graphs/ds_demo?graph_version={first_graph_version}")

    assert failed.status_code == 202
    assert failed.json()["status"] == "running"
    assert status.status_code == 200
    assert first_result["status"] == "running"
    assert status.json()["current_graph_version"] == first_graph_version
    assert status.json()["current_build"]["status"] == "failed"
    assert status.json()["last_error_code"] == expected_error_code.value
    assert status.json()["last_error_message"]
    assert previous_graph.status_code == 200
    assert previous_graph.json()["graph_version"] == first_graph_version


def test_rest_rebuild_embeds_tables_and_columns_without_profile_values(tmp_path: Path) -> None:
    """语义词找不到时，表和字段也必须能参与向量召回，且索引文本不含画像取值。"""

    source_root = tmp_path / "datasources"
    source_dir = source_root / "demo"
    source_dir.mkdir(parents=True)
    demo_database = Path(__file__).parents[3] / "data" / "demo" / "ecommerce.sqlite"
    shutil.copy2(demo_database, source_dir / "ecommerce.sqlite")
    settings = Settings(
        source_root=source_root,
        database_path=tmp_path / "datalink" / "datalink.db",
        embedding_model="test-embedding",
    )
    semantic_client = EmbeddingSemanticClient()

    with TestClient(create_app(settings, semantic_client=semantic_client)) as client:
        rebuild = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "ds_demo",
                "rebuild_key": "rebuild_with_embeddings",
                "source_type": DataSourceType.SQLITE.value,
                "source_ref": "demo/ecommerce.sqlite",
                "schema_revision": 1,
            },
        )
        assert rebuild.status_code == 202
        build_id = rebuild.json()["build_id"]
        repository = client.app.state.graph_repository
        snapshot = repository.get_snapshot(repository.get_build(build_id))
        embeddings = repository.get_embeddings(build_id)

    assert {embedding.node_id for embedding in embeddings} == {node.id for node in snapshot.nodes}
    assert {node.type for node in snapshot.nodes} == {
        DataLinkNodeType.TABLE,
        DataLinkNodeType.COLUMN,
        DataLinkNodeType.CONCEPT,
        DataLinkNodeType.ENTITY,
    }
    assert semantic_client.embedding_texts
    assert all("@" not in text for text in semantic_client.embedding_texts)


def test_rest_rebuild_returns_active_build_without_waiting_or_replacing_it(tmp_path: Path) -> None:
    """重复请求只返回活动 Build，不把“仍在执行”伪装成执行失败。"""

    settings = Settings(
        source_root=tmp_path / "datasources",
        database_path=tmp_path / "datalink" / "datalink.db",
    )
    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        active_build = client.app.state.graph_repository.claim_build("ds_active", 1).build
        response = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "ds_active",
                "rebuild_key": "rebuild_2",
                "source_type": DataSourceType.CSV.value,
                "source_ref": "active/source.csv",
                "schema_revision": 1,
            },
        )
        preserved = client.app.state.graph_repository.get_build(active_build.id)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": DataLinkErrorCode.BUILD_ALREADY_RUNNING.value,
        "message": "Graph build is already running",
        "details": {"build_id": active_build.id},
    }
    assert preserved is not None
    assert preserved.status.value == "running"
    assert preserved.id == active_build.id


def test_rest_rebuild_reuses_same_key_without_second_background_submission(tmp_path: Path) -> None:
    """同一个网络重试只能复用 running Build，不能重复排入后台执行。"""

    settings = Settings(
        source_root=tmp_path / "datasources",
        database_path=tmp_path / "datalink" / "datalink.db",
    )
    submitted_build_ids: list[str] = []

    def keep_build_running(
        request: DataLinkRebuildRequest,
        claim: BuildClaim,
    ) -> None:
        del request
        submitted_build_ids.append(claim.build.id)

    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        client.app.state.build_service.execute_submitted_rebuild = keep_build_running
        payload = {
            "datasource_id": "ds_retry",
            "rebuild_key": "same_network_request",
            "source_type": DataSourceType.CSV.value,
            "source_ref": "retry/source.csv",
            "schema_revision": 1,
        }

        first = client.post("/v1/graphs/rebuild", json=payload)
        retry = client.post("/v1/graphs/rebuild", json=payload)
        status = client.get("/v1/graphs/ds_retry/status")

    assert first.status_code == 202
    assert retry.status_code == 202
    assert first.json()["status"] == DataLinkBuildStatus.RUNNING.value
    assert retry.json() == first.json()
    assert submitted_build_ids == [first.json()["build_id"]]
    assert status.json()["current_build"]["status"] == DataLinkBuildStatus.RUNNING.value


def test_rest_rebuild_rejects_path_escape_with_stable_error_code(tmp_path: Path) -> None:
    """管理接口拒绝越界 source_ref，并且不把解析路径回显给调用方。"""

    settings = Settings(
        source_root=tmp_path / "datasources",
        database_path=tmp_path / "datalink" / "datalink.db",
    )
    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        response = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "ds_demo",
                "rebuild_key": "rebuild_1",
                "source_type": DataSourceType.SQLITE.value,
                "source_ref": "../outside.sqlite",
                "schema_revision": 1,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PATH_OUTSIDE_ROOT"
    assert "outside.sqlite" not in response.text


def test_rebuild_candidate_response_preserves_publication_state_on_retry(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    csv = source_root / "data.csv"
    csv.write_text("id,amount\n1,100\n", encoding="utf-8")
    settings = Settings(source_root=source_root, database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings, semantic_client=DemoSemanticClient())) as client:
        service = client.app.state.build_service
        repo = client.app.state.graph_repository
        first = service.rebuild(
            DataLinkRebuildRequest(
                datasource_id="ds",
                rebuild_key="first",
                source_type="csv",
                source_ref="data.csv",
                schema_revision=1,
            )
        )
        snapshot = repo.get_snapshot(repo.get_completed_build("ds", first.graph_version))
        column = next(
            n for n in snapshot.nodes if n.type == DataLinkNodeType.COLUMN and n.name == "amount"
        )
        revisions = client.app.state.revision_service
        revisions.save_draft(
            "ds",
            DataLinkDraftSaveRequest(
                base_graph_version=first.graph_version,
                schema_revision=1,
                changes=[
                    {
                        "change_type": "update_node",
                        "object_key": column.id,
                        "description": "Curated amount",
                    }
                ],
            ),
        )
        published = revisions.publish(
            "ds",
            DataLinkPublishRequest(
                expected_head=first.graph_version,
                expected_draft_revision=1,
                idempotency_key="publish",
            ),
        )
        csv.write_text("id,total\n1,100\n", encoding="utf-8")
        request = DataLinkRebuildRequest(
            datasource_id="ds",
            rebuild_key="second",
            source_type="csv",
            source_ref="data.csv",
            schema_revision=2,
        )
        rebuilt = service.rebuild(request)
        assert rebuilt.status == DataLinkBuildStatus.COMPLETED
        assert rebuilt.publication_state == "candidate"
        assert service.rebuild(request) == rebuilt
        response = client.post("/v1/graphs/rebuild", json=request.model_dump(mode="json"))
        assert response.json()["publication_state"] == "candidate"
        assert response.json()["status"] == "completed"
        assert repo.get_head("ds").current_graph_version == published.graph_version
        assert repo.get_completed_build("ds", rebuilt.graph_version) is None


def test_mcp_registers_only_the_confirmed_explore_tool(tmp_path: Path) -> None:
    """MCP 不意外暴露搜索、删图或建图等辅助/管理工具。"""

    app = create_app(
        Settings(
            source_root=tmp_path / "datasources",
            database_path=tmp_path / "datalink" / "datalink.db",
        ),
        semantic_client=DemoSemanticClient(),
    )

    tools = asyncio.run(app.state.mcp_server.list_tools())

    assert [tool.name for tool in tools] == ["datalink_explore"]


@pytest.mark.parametrize(
    ("focus", "max_nodes"),
    [("not_a_focus", 12), (None, 51)],
)
def test_mcp_maps_invalid_explore_parameters_to_invalid_query(
    tmp_path: Path, focus: str | None, max_nodes: int
) -> None:
    repository = make_repository(tmp_path)
    tool = asyncio.run(create_mcp_server(GraphExplorer(repository)).get_tool("datalink_explore"))
    assert tool is not None

    with pytest.raises(ValueError, match="^INVALID_QUERY:"):
        tool.fn(
            datasource_id="ds_demo",
            graph_version="graph-1",
            query="orders",
            focus=focus,
            max_nodes=max_nodes,
        )


def _table(name: str, *, datasource_id: str = "ds_demo") -> GraphNode:
    return GraphNode(
        id=f"table:{datasource_id}:{name}",
        type=DataLinkNodeType.TABLE,
        name=name,
    )


def _column(table_name: str, column_name: str, *, datasource_id: str = "ds_demo") -> GraphNode:
    return GraphNode(
        id=f"column:{datasource_id}:{table_name}:{column_name}",
        type=DataLinkNodeType.COLUMN,
        name=column_name,
        table_name=table_name,
    )


def _contains(table_name: str, column_name: str, *, datasource_id: str = "ds_demo") -> GraphEdge:
    return GraphEdge(
        id=f"edge:contains:{datasource_id}:{table_name}:{column_name}",
        source_id=f"table:{datasource_id}:{table_name}",
        target_id=f"column:{datasource_id}:{table_name}:{column_name}",
        type=DataLinkEdgeType.CONTAINS,
        confidence=1.0,
    )


def _concept(name: str, description: str, column_id: str) -> GraphNode:
    return GraphNode(
        id=f"concept:ds_loans:{name}",
        type=DataLinkNodeType.CONCEPT,
        name=name,
        description=description,
        properties={"column_id": column_id},
    )


def _represents(column_id: str, concept_id: str) -> GraphEdge:
    return GraphEdge(
        id=f"edge:represents:{column_id}:{concept_id}",
        source_id=column_id,
        target_id=concept_id,
        type=DataLinkEdgeType.REPRESENTS,
        confidence=0.9,
    )


def _has_concept(entity_id: str, concept_id: str) -> GraphEdge:
    return GraphEdge(
        id=f"edge:has-concept:{entity_id}:{concept_id}",
        source_id=entity_id,
        target_id=concept_id,
        type=DataLinkEdgeType.HAS_CONCEPT,
        confidence=0.9,
    )


def _profile(
    table_name: str, column_name: str, *, top_values: tuple[str, ...] = ()
) -> ColumnProfile:
    return ColumnProfile(
        id=f"profile:column:ds_demo:{table_name}:{column_name}",
        column_id=f"column:ds_demo:{table_name}:{column_name}",
        column_name=column_name,
        dtype="text",
        null_rate=0.0,
        distinct_count=2,
        unique_rate=1.0,
        top_values=top_values,
        sample_values=top_values,
    )
