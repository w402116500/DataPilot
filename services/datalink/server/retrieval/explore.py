"""为 REST 与 MCP 提供同一套版本隔离、脱敏且有大小上限的语义检索。"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import combinations, count
from typing import Literal

from contracts.datalink import (
    DataLinkBrowserNodeRead,
    DataLinkBrowserProfileRead,
    DataLinkColumnProfileRead,
    DataLinkEdgeRead,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkExploreRequest,
    DataLinkExploreResult,
    DataLinkGraphEntriesRead,
    DataLinkGraphEntryRead,
    DataLinkGraphEntryType,
    DataLinkGraphRead,
    DataLinkJoinPathRead,
    DataLinkJoinPathStepRead,
    DataLinkNodeRead,
    DataLinkNodeType,
    DataLinkSubgraphRead,
)
from contracts.sensitive_fields import SensitiveFieldPolicy

from server.graph.repository import GraphRepository, GraphSnapshot
from server.mapper.client import (
    ModelConfigurationError,
    ModelResponseError,
    SemanticModelClient,
)
from server.models.graph import GraphEdge, GraphNode, edge_enabled, graph_provenance
from server.models.profile import ColumnProfile
from server.retrieval.catalog import SemanticCatalog

_MAX_JOIN_HOPS = 3
_MAX_JOIN_PATHS = 10
_MAX_BROWSER_HOPS = 2
_MAX_BROWSER_NODES = 80
_MAX_BROWSER_EDGES = 150
_MAX_SEMANTIC_CONTEXT_NODES = 4
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_QUERY_EMBEDDING_FAILED_WARNING = (
    "Stored embeddings exist but query embedding failed; results use keyword matching only"
)
_JOIN_TYPES = frozenset({DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE})
_ENTRY_NODE_TYPES = {
    "table": DataLinkNodeType.TABLE,
    "entity": DataLinkNodeType.ENTITY,
}


@dataclass(frozen=True)
class GraphAccessError(RuntimeError):
    """访问不到指定完成图谱时的稳定且不泄露内部信息的错误。"""

    code: DataLinkErrorCode
    message: str


@dataclass(frozen=True)
class _ScoredNode:
    """关键词或向量召回后的内部排序项。"""

    node: GraphNode
    score: float


class GraphExplorer:
    """在一个明确完成版本内做有限关键词、可选向量与关系检索。"""

    def __init__(
        self,
        repository: GraphRepository,
        model_client: SemanticModelClient | None = None,
        sensitive_policy: SensitiveFieldPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.model_client = model_client
        self.sensitive_policy = sensitive_policy or SensitiveFieldPolicy()

    def explore(self, request: DataLinkExploreRequest) -> DataLinkExploreResult:
        """按受控 query 返回最多五十个节点和十条可解释 Join 路径。"""

        snapshot = self._snapshot_for(request.datasource_id, request.graph_version)
        return self.explore_snapshot(snapshot, request, use_embeddings=True)

    def explore_snapshot(
        self,
        snapshot: GraphSnapshot,
        request: DataLinkExploreRequest,
        *,
        use_embeddings: bool = False,
    ) -> DataLinkExploreResult:
        """Management preview uses the same traversal, without querying embeddings."""

        profiles_by_column = {profile.column_id: profile for profile in snapshot.profiles}
        scored = self._keyword_recall(request.query, snapshot.nodes, profiles_by_column)
        retrieval_mode = "keyword"
        vector_warning: str | None = None
        if use_embeddings:
            scored, retrieval_mode, vector_warning = self._add_vector_recall(
                request.query, snapshot, scored
            )
        active_edges = tuple(edge for edge in snapshot.edges if edge_enabled(edge))
        selected, nodes_truncated = self._expand_neighbors(
            request.query,
            scored,
            snapshot.nodes,
            active_edges,
            request.focus,
            request.max_nodes,
        )
        selected_ids = {node.id for node in selected}
        visible_edge_types = _visible_edge_types_for_focus(request.focus)
        matching_edges = tuple(
            edge
            for edge in active_edges
            if (
                edge.type in visible_edge_types
                and edge.source_id in selected_ids
                and edge.target_id in selected_ids
            )
        )
        edges = matching_edges[:100]
        join_paths = self._join_paths(selected, snapshot.nodes, active_edges)
        warnings = [] if selected else ["No matching nodes were found in this graph version"]
        if vector_warning:
            warnings.append(vector_warning)
        if any(path.confidence is None for path in join_paths):
            warnings.append(
                "Manual candidate paths are unverified and have no statistical confidence"
            )
        return DataLinkExploreResult(
            datasource_id=request.datasource_id,
            graph_version=request.graph_version,
            query=request.query,
            nodes=[self._node_read(node, profiles_by_column.get(node.id)) for node in selected],
            edges=[_edge_read(edge) for edge in edges],
            join_paths=list(join_paths),
            warnings=warnings,
            retrieval_mode=retrieval_mode,
            is_truncated=nodes_truncated or len(matching_edges) > 100,
        )

    def read_graph(self, datasource_id: str, graph_version: str) -> DataLinkGraphRead:
        """读取旧整图接口的安全地图摘要，仍只允许已完成版本。"""

        snapshot = self._snapshot_for(datasource_id, graph_version)
        profiles_by_column = {profile.column_id: profile for profile in snapshot.profiles}
        return DataLinkGraphRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            nodes=[
                self._browser_node_read(node, profiles_by_column.get(node.id))
                for node in snapshot.nodes[:100]
            ],
            edges=[_edge_read(edge) for edge in snapshot.edges][:200],
            warnings=[],
        )

    def list_browser_entries(
        self,
        datasource_id: str,
        graph_version: str,
        *,
        entry_type: DataLinkGraphEntryType | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkGraphEntriesRead:
        """按完成图谱版本返回表或实体入口，供页面选择局部图根节点。"""

        snapshot = self._snapshot_for(datasource_id, graph_version)
        normalized_query = query.strip().casefold() if query is not None else ""
        allowed_types = (
            frozenset({_ENTRY_NODE_TYPES[entry_type]})
            if entry_type is not None
            else frozenset(_ENTRY_NODE_TYPES.values())
        )
        entries = tuple(
            node
            for node in snapshot.nodes
            if node.type in allowed_types and _matches_entry_query(node, normalized_query)
        )
        ordered_entries = tuple(
            sorted(entries, key=lambda node: (node.type.value, node.name, node.id))
        )
        start = (page - 1) * page_size
        visible_entries = ordered_entries[start : start + page_size]
        return DataLinkGraphEntriesRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            items=[
                DataLinkGraphEntryRead(
                    id=node.id,
                    type=_entry_type_for_node(node),
                    name=node.name,
                    description=node.description,
                    aliases=list(node.aliases),
                )
                for node in visible_entries
            ],
            page=page,
            page_size=page_size,
            total=len(ordered_entries),
        )

    def read_browser_subgraph(
        self,
        datasource_id: str,
        graph_version: str,
        *,
        root_node_id: str,
        edge_types: Iterable[DataLinkEdgeType] | None = None,
        hops: int = 1,
    ) -> DataLinkSubgraphRead:
        """以已验证根节点读取一到两跳局部图，并明确返回展示上限截断事实。"""

        snapshot = self._snapshot_for(datasource_id, graph_version)
        node_by_id = {node.id: node for node in snapshot.nodes}
        if root_node_id not in node_by_id:
            raise GraphAccessError(
                DataLinkErrorCode.INVALID_QUERY,
                "Requested root node is unavailable in this graph version",
            )
        if hops < 1 or hops > _MAX_BROWSER_HOPS:
            raise GraphAccessError(
                DataLinkErrorCode.INVALID_QUERY,
                "Subgraph hops must be between 1 and 2",
            )

        visible_edge_types = (
            frozenset(edge_types) if edge_types is not None else frozenset(DataLinkEdgeType)
        )
        # Table roots reach join evidence through their column containment edges.
        # Keep those edges as an internal traversal bridge even when the caller
        # only wants to draw foreign keys or inferred join candidates.
        traversal_edge_types = visible_edge_types | {DataLinkEdgeType.CONTAINS}
        traversal_edges = tuple(
            edge for edge in snapshot.edges if edge.type in traversal_edge_types
        )
        selected_node_ids, node_limit_reached = _expand_subgraph_nodes(
            root_node_id,
            node_by_id,
            traversal_edges,
            hops=hops,
            max_nodes=_MAX_BROWSER_NODES,
        )
        selected_edges = tuple(
            edge
            for edge in snapshot.edges
            if edge.type in visible_edge_types
            if edge.source_id in selected_node_ids and edge.target_id in selected_node_ids
        )
        edge_limit_reached = len(selected_edges) > _MAX_BROWSER_EDGES
        visible_edges = selected_edges[:_MAX_BROWSER_EDGES]
        is_truncated = node_limit_reached or edge_limit_reached
        warnings = (
            ["局部图已达到展示上限，仅显示当前根节点附近的部分关系。"] if is_truncated else []
        )
        profiles_by_column = {profile.column_id: profile for profile in snapshot.profiles}
        selected_nodes = tuple(node for node in snapshot.nodes if node.id in selected_node_ids)
        return DataLinkSubgraphRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            root_node_id=root_node_id,
            total_node_count=len(snapshot.nodes),
            total_edge_count=len(snapshot.edges),
            nodes=[
                self._browser_node_read(node, profiles_by_column.get(node.id))
                for node in selected_nodes
            ],
            edges=[_edge_read(edge) for edge in visible_edges],
            is_truncated=is_truncated,
            warnings=warnings,
        )

    def catalog(self, datasource_id: str, graph_version: str) -> SemanticCatalog:
        """Build the safe catalog from the entire validated version snapshot."""

        return SemanticCatalog(
            self._snapshot_for(datasource_id, graph_version), self._browser_node_read
        )

    def catalog_snapshot(self, snapshot: GraphSnapshot) -> SemanticCatalog:
        """Project an explicitly authorized management snapshot with the same safe catalog."""
        return SemanticCatalog(snapshot, self._browser_node_read)

    def _snapshot_for(self, datasource_id: str, graph_version: str) -> GraphSnapshot:
        """确认版本属于请求数据源且已经完成，禁止检索 running/failed 图谱。"""

        build = self.repository.get_completed_build(datasource_id, graph_version)
        if build is not None:
            return self.repository.get_snapshot(build)
        version_owner = self.repository.get_build_by_graph_version(graph_version)
        if version_owner is not None and version_owner.datasource_id != datasource_id:
            raise GraphAccessError(
                DataLinkErrorCode.DATASOURCE_MISMATCH,
                "Graph version does not belong to this data source",
            )
        raise GraphAccessError(
            DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND,
            "Requested graph version is unavailable",
        )

    def _keyword_recall(
        self,
        query: str,
        nodes: Sequence[GraphNode],
        profiles_by_column: dict[str, ColumnProfile],
    ) -> tuple[_ScoredNode, ...]:
        """只在展示元数据和脱敏画像中关键词召回，完全不读取 Connector 原始行。"""

        normalized_query = query.casefold()
        tokens = tuple(dict.fromkeys(_TOKEN_PATTERN.findall(normalized_query)))
        scored: list[_ScoredNode] = []
        for node in nodes:
            profile = profiles_by_column.get(node.id)
            searchable = " ".join(
                part
                for part in (
                    node.name,
                    node.table_name or "",
                    node.semantic_type or "",
                    node.description or "",
                    *node.aliases,
                    profile.semantic_type if profile else "",
                )
                if part
            ).casefold()
            score = 0.0
            if normalized_query in searchable:
                score += 3.0
            score += sum(1.0 for token in tokens if token in searchable)
            if node.name.casefold() == normalized_query:
                score += 2.0
            if any(_term_appears_in_query(term, query) for term in (node.name, *node.aliases)):
                score += 2.0
            if score:
                scored.append(_ScoredNode(node=node, score=score))
        return _sorted_scored(scored)

    def _add_vector_recall(
        self,
        query: str,
        snapshot: GraphSnapshot,
        keyword_matches: Sequence[_ScoredNode],
    ) -> tuple[tuple[_ScoredNode, ...], Literal["keyword", "hybrid"], str | None]:
        """有已存向量且模型可用时补召回；库存向量存在但 query 向量失败时回退关键词并告警。"""

        if self.model_client is None:
            return tuple(keyword_matches), "keyword", None
        embeddings = self.repository.get_embeddings(snapshot.build.id)
        if not embeddings:
            return tuple(keyword_matches), "keyword", None
        try:
            query_vectors = self.model_client.embed((query,))
        except (ModelConfigurationError, ModelResponseError, KeyError, TypeError, ValueError):
            return tuple(keyword_matches), "keyword", _QUERY_EMBEDDING_FAILED_WARNING
        if query_vectors is None or len(query_vectors) != 1:
            return tuple(keyword_matches), "keyword", _QUERY_EMBEDDING_FAILED_WARNING
        node_by_id = {node.id: node for node in snapshot.nodes}
        merged = {match.node.id: match for match in keyword_matches}
        for embedding in embeddings:
            node = node_by_id.get(embedding.node_id)
            if node is None:
                continue
            similarity = _cosine_similarity(query_vectors[0], embedding.vector)
            if similarity <= 0:
                continue
            current = merged.get(node.id)
            base_score = similarity * (0.6 if keyword_matches else 0.8)
            score = base_score * _vector_node_weight(node.type)
            if current is None or score > current.score:
                merged[node.id] = _ScoredNode(node=node, score=score)
        return _sorted_scored(merged.values()), "hybrid", None

    def _expand_neighbors(
        self,
        query: str,
        scored: Sequence[_ScoredNode],
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        focus: str | None,
        max_nodes: int,
    ) -> tuple[tuple[GraphNode, ...], bool]:
        """先把语义命中展开为字段，再用有限预算补关系邻居。"""

        node_by_id = {node.id: node for node in nodes}
        tables_by_name = {node.name: node for node in nodes if node.type == DataLinkNodeType.TABLE}
        semantic_scores: dict[str, _ScoredNode] = {item.node.id: item for item in scored}
        semantic_column_scores: dict[str, _ScoredNode] = {}
        represents_by_concept: dict[str, list[GraphEdge]] = {}
        represents_by_column: dict[str, list[GraphEdge]] = {}
        concepts_by_entity: dict[str, list[GraphEdge]] = {}
        entities_by_concept: dict[str, list[GraphEdge]] = {}
        for edge in edges:
            if edge.type == DataLinkEdgeType.REPRESENTS:
                represents_by_concept.setdefault(edge.target_id, []).append(edge)
                represents_by_column.setdefault(edge.source_id, []).append(edge)
            elif edge.type == DataLinkEdgeType.HAS_CONCEPT:
                concepts_by_entity.setdefault(edge.source_id, []).append(edge)
                entities_by_concept.setdefault(edge.target_id, []).append(edge)

        direct_entity_ids = _direct_entity_ids(query, semantic_scores.values())
        direct_entity_concept_ids = {
            edge.target_id
            for entity_id in direct_entity_ids
            for edge in concepts_by_entity.get(entity_id, [])
            if (concept := node_by_id.get(edge.target_id)) is not None
            and concept.type == DataLinkNodeType.CONCEPT
        }

        # Concept / Entity 是中文语义召回的索引，不应在截断前挤掉真实字段。
        for item in tuple(semantic_scores.values()):
            if item.node.type == DataLinkNodeType.CONCEPT:
                for represents in represents_by_concept.get(item.node.id, []):
                    _add_semantic_column(
                        semantic_scores,
                        semantic_column_scores,
                        node_by_id.get(represents.source_id),
                        _mapped_score(item.score, represents.confidence) * 0.6,
                    )
            elif item.node.type == DataLinkNodeType.ENTITY:
                for has_concept in concepts_by_entity.get(item.node.id, []):
                    concept = node_by_id.get(has_concept.target_id)
                    if concept is None or concept.type != DataLinkNodeType.CONCEPT:
                        continue
                    for represents in represents_by_concept.get(concept.id, []):
                        _add_semantic_column(
                            semantic_scores,
                            semantic_column_scores,
                            node_by_id.get(represents.source_id),
                            _mapped_score(item.score, has_concept.confidence, represents.confidence)
                            * 0.4,
                        )

        for item in tuple(semantic_scores.values()):
            if item.node.type != DataLinkNodeType.COLUMN or item.node.table_name is None:
                continue
            table = tables_by_name.get(item.node.table_name)
            _add_scored_node(semantic_scores, table, item.score * 0.5)

        selected: dict[str, GraphNode] = {}
        semantic_budget = 0
        if focus != "join_paths":
            default_semantic_budget = min(_MAX_SEMANTIC_CONTEXT_NODES, max_nodes // 3)
            direct_entity_budget = min(
                len(direct_entity_ids) + len(direct_entity_concept_ids),
                max_nodes // 2,
            )
            semantic_budget = max(default_semantic_budget, direct_entity_budget)
        physical_budget = max_nodes - semantic_budget
        selected = _select_physical_nodes(
            semantic_columns=semantic_column_scores.values(),
            scored=semantic_scores.values(),
            tables_by_name=tables_by_name,
            max_nodes=physical_budget,
        )
        semantic_context = (
            _semantic_context_for_selected_columns(
                selected,
                semantic_scores,
                node_by_id,
                represents_by_column,
                entities_by_concept,
            )
            if focus != "join_paths"
            else {}
        )
        if focus != "join_paths":
            _add_direct_entity_context(
                semantic_context,
                direct_entity_ids,
                semantic_scores,
                node_by_id,
                concepts_by_entity,
            )
        primary_columns = tuple(
            node for node in selected.values() if node.type == DataLinkNodeType.COLUMN
        )[:semantic_budget]
        semantic_context_for_primary_columns = (
            _semantic_context_for_selected_columns(
                {node.id: node for node in primary_columns},
                semantic_scores,
                node_by_id,
                represents_by_column,
                entities_by_concept,
            )
            if focus != "join_paths"
            else {}
        )
        selected_semantic_count = 0
        for candidates in (
            semantic_context_for_primary_columns.values(),
            semantic_context.values(),
        ):
            for item in _semantic_scored(candidates, preferred_node_ids=direct_entity_ids):
                if len(selected) >= max_nodes or selected_semantic_count >= semantic_budget:
                    break
                if item.node.id in selected:
                    continue
                selected[item.node.id] = item.node
                selected_semantic_count += 1
            if len(selected) >= max_nodes or selected_semantic_count >= semantic_budget:
                break
        for item in _physical_scored(semantic_scores.values()):
            if len(selected) >= max_nodes:
                break
            selected[item.node.id] = item.node

        allowed_types = _edge_types_for_focus(focus)
        eligible_ids = {
            node_id
            for node_id, item in semantic_scores.items()
            if focus != "join_paths"
            or item.node.type in {DataLinkNodeType.TABLE, DataLinkNodeType.COLUMN}
        }
        truncated = bool(eligible_ids - set(selected))
        for edge in sorted(edges, key=_edge_rank):
            if (
                len(selected) >= max_nodes
                and edge.type in allowed_types
                and ((edge.source_id in selected) != (edge.target_id in selected))
            ):
                truncated = True
            if len(selected) >= max_nodes or edge.type not in allowed_types:
                continue
            source = selected.get(edge.source_id)
            target = selected.get(edge.target_id)
            if source is not None and target is None:
                neighbor = node_by_id.get(edge.target_id)
                if neighbor is not None:
                    selected[neighbor.id] = neighbor
            if target is not None and source is None:
                neighbor = node_by_id.get(edge.source_id)
                if neighbor is not None:
                    selected[neighbor.id] = neighbor
        return tuple(selected.values()), truncated

    def _join_paths(
        self,
        selected: Sequence[GraphNode],
        all_nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
    ) -> tuple[DataLinkJoinPathRead, ...]:
        """在表级投影中优先检索外键和统计候选，语义说明关系不会进入路径。"""

        node_by_id = {node.id: node for node in all_nodes}
        join_edges = tuple(
            edge
            for edge in edges
            if edge.type in _JOIN_TYPES and _column_table_pair(edge, node_by_id) is not None
        )
        selected_tables = tuple(
            dict.fromkeys(
                table_name
                for node in selected
                for table_name in (_table_name(node),)
                if table_name is not None
            )
        )
        if not join_edges or not selected_tables:
            return ()
        candidate_pairs = list(combinations(selected_tables, 2))
        if len(selected_tables) == 1:
            source_table = selected_tables[0]
            neighbors = tuple(
                _other_table(edge, source_table, node_by_id)
                for edge in join_edges
                if _other_table(edge, source_table, node_by_id) is not None
            )
            candidate_pairs = [(source_table, target) for target in dict.fromkeys(neighbors)]

        paths: list[DataLinkJoinPathRead] = []
        for source_table, target_table in candidate_pairs:
            paths.extend(
                _bfs_join_paths(
                    source_table,
                    target_table,
                    join_edges,
                    node_by_id,
                    max_paths=_MAX_JOIN_PATHS,
                )
            )
        return tuple(sorted(paths, key=_path_rank)[:_MAX_JOIN_PATHS])

    def _node_read(self, node: GraphNode, profile: ColumnProfile | None) -> DataLinkNodeRead:
        """最后一道输出脱敏检查，防止错误历史数据因读取而泄露。"""

        return DataLinkNodeRead(
            id=node.id,
            type=node.type,
            name=node.name,
            table=node.table_name,
            semantic_type=node.semantic_type,
            description=node.description,
            aliases=list(node.aliases),
            profile=self._safe_profile(node, profile),
            provenance=graph_provenance(node),
        )

    def _browser_node_read(
        self, node: GraphNode, profile: ColumnProfile | None
    ) -> DataLinkBrowserNodeRead:
        """把内部画像投影为地图安全统计，不复用 Agent 的可检索节点 DTO。"""

        return DataLinkBrowserNodeRead(
            id=node.id,
            type=node.type,
            name=node.name,
            table=node.table_name,
            semantic_type=node.semantic_type,
            description=node.description,
            aliases=list(node.aliases),
            profile=_browser_profile_read(profile),
            provenance=graph_provenance(node),
        )

    def _safe_profile(
        self, node: GraphNode, profile: ColumnProfile | None
    ) -> DataLinkColumnProfileRead | None:
        """按字段名复核敏感策略，敏感列即使旧库漏标也不会读出实际取值。"""

        if profile is None:
            return None
        value = profile.to_read()
        if not self.sensitive_policy.is_sensitive_field(node.name):
            return value
        return value.model_copy(
            update={"top_values": [], "sample_values": [], "min_value": None, "max_value": None}
        )


def _matches_entry_query(node: GraphNode, query: str) -> bool:
    """入口搜索只匹配可展示元数据，不读取画像、向量或原始数据。"""

    if not query:
        return True
    searchable = " ".join(
        part for part in (node.name, node.description or "", *node.aliases) if part
    ).casefold()
    return query in searchable


def _entry_type_for_node(node: GraphNode) -> DataLinkGraphEntryType:
    """把已筛选的内部节点类型映射到浏览器入口类型。"""

    if node.type == DataLinkNodeType.TABLE:
        return "table"
    if node.type == DataLinkNodeType.ENTITY:
        return "entity"
    raise ValueError("Only table and entity nodes can be graph browser entries")


def _browser_profile_read(profile: ColumnProfile | None) -> DataLinkBrowserProfileRead | None:
    """浏览器仅需理解字段结构，不应接触建图使用的样例和数值范围。"""

    if profile is None:
        return None
    return DataLinkBrowserProfileRead(
        dtype=profile.dtype,
        semantic_type=profile.semantic_type,
        null_rate=profile.null_rate,
        distinct_count=profile.distinct_count,
        unique_rate=profile.unique_rate,
    )


def _expand_subgraph_nodes(
    root_node_id: str,
    node_by_id: dict[str, GraphNode],
    edges: Sequence[GraphEdge],
    *,
    hops: int,
    max_nodes: int,
) -> tuple[set[str], bool]:
    """按边类型从根节点做有限 BFS，根节点始终保留且不把大图整张加载进结果。"""

    adjacency: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        if edge.source_id not in node_by_id or edge.target_id not in node_by_id:
            continue
        adjacency.setdefault(edge.source_id, []).append(edge)
        adjacency.setdefault(edge.target_id, []).append(edge)

    selected = {root_node_id}
    queue: deque[tuple[str, int]] = deque([(root_node_id, 0)])
    node_limit_reached = False
    while queue:
        current_node_id, depth = queue.popleft()
        if depth >= hops:
            continue
        for edge in adjacency.get(current_node_id, []):
            neighbor_id = edge.target_id if edge.source_id == current_node_id else edge.source_id
            if neighbor_id in selected:
                continue
            if len(selected) >= max_nodes:
                node_limit_reached = True
                continue
            selected.add(neighbor_id)
            queue.append((neighbor_id, depth + 1))
    return selected, node_limit_reached


def _sorted_scored(items: Iterable[_ScoredNode]) -> tuple[_ScoredNode, ...]:
    """固定相关性、节点类型、名称排序，保证 Demo 检索结果可重复。"""

    return tuple(
        sorted(
            items,
            key=lambda item: (-item.score, item.node.type.value, item.node.name, item.node.id),
        )
    )


def _add_scored_node(scored: dict[str, _ScoredNode], node: GraphNode | None, score: float) -> None:
    """按最高相关性合并同一节点的多条语义展开路径。"""

    if node is None:
        return
    current = scored.get(node.id)
    if current is None or score > current.score:
        scored[node.id] = _ScoredNode(node=node, score=score)


def _add_semantic_column(
    scored: dict[str, _ScoredNode],
    semantic_columns: dict[str, _ScoredNode],
    node: GraphNode | None,
    score: float,
) -> None:
    """记录概念命中的真实字段，供节点预算优先保留。"""

    _add_scored_node(scored, node, score)
    if node is not None and node.type == DataLinkNodeType.COLUMN:
        _add_scored_node(semantic_columns, node, score)


def _select_physical_nodes(
    *,
    semantic_columns: Iterable[_ScoredNode],
    scored: Iterable[_ScoredNode],
    tables_by_name: Mapping[str, GraphNode],
    max_nodes: int,
) -> dict[str, GraphNode]:
    """先保留概念实际指向的字段，再补直接召回的字段和表。"""

    selected: dict[str, GraphNode] = {}
    semantic_candidates = tuple(
        item
        for item in _physical_scored(semantic_columns)
        if item.node.type == DataLinkNodeType.COLUMN
    )
    semantic_column_budget = min(len(semantic_candidates), max(1, max_nodes // 2))

    for item in semantic_candidates[:semantic_column_budget]:
        _select_column_with_table(selected, item.node, tables_by_name, max_nodes)

    for item in _physical_scored(scored):
        if item.node.type != DataLinkNodeType.COLUMN:
            continue
        _select_column_with_table(selected, item.node, tables_by_name, max_nodes)

    for item in _physical_scored(scored):
        if len(selected) >= max_nodes:
            break
        if item.node.type == DataLinkNodeType.TABLE:
            selected.setdefault(item.node.id, item.node)
    return selected


def _select_column_with_table(
    selected: dict[str, GraphNode],
    column: GraphNode,
    tables_by_name: Mapping[str, GraphNode],
    max_nodes: int,
) -> None:
    """字段优先；只在预算允许时补回它所属的表。"""

    if column.id in selected or len(selected) >= max_nodes:
        return
    selected[column.id] = column
    if column.table_name is None or len(selected) >= max_nodes:
        return
    table = tables_by_name.get(column.table_name)
    if table is not None:
        selected.setdefault(table.id, table)


def _semantic_context_for_selected_columns(
    selected: dict[str, GraphNode],
    scored: dict[str, _ScoredNode],
    node_by_id: dict[str, GraphNode],
    represents_by_column: dict[str, list[GraphEdge]],
    entities_by_concept: dict[str, list[GraphEdge]],
) -> dict[str, _ScoredNode]:
    """为已选字段补回可解释的概念和实体，不让无关语义节点占用预算。"""

    context: dict[str, _ScoredNode] = {}
    for column in selected.values():
        if column.type != DataLinkNodeType.COLUMN:
            continue
        column_score = scored.get(column.id)
        if column_score is None:
            continue
        for represents in represents_by_column.get(column.id, []):
            concept = node_by_id.get(represents.target_id)
            if concept is None or concept.type != DataLinkNodeType.CONCEPT:
                continue
            concept_score = max(
                _mapped_score(column_score.score, represents.confidence),
                scored.get(concept.id, _ScoredNode(concept, 0.0)).score,
            )
            _add_scored_node(context, concept, concept_score)
            for has_concept in entities_by_concept.get(concept.id, []):
                entity = node_by_id.get(has_concept.source_id)
                if entity is None or entity.type != DataLinkNodeType.ENTITY:
                    continue
                entity_score = max(
                    _mapped_score(concept_score, has_concept.confidence),
                    scored.get(entity.id, _ScoredNode(entity, 0.0)).score,
                )
                _add_scored_node(context, entity, entity_score)
    return context


def _term_appears_in_query(term: str, query: str) -> bool:
    """名称或别名完整出现在问句：非 ASCII 用子串，ASCII 必须等于 \\w+ token。"""

    normalized_term = term.casefold()
    if not normalized_term:
        return False
    normalized_query = query.casefold()
    if normalized_term.isascii():
        return normalized_term in frozenset(_TOKEN_PATTERN.findall(normalized_query))
    return normalized_term in normalized_query


def _direct_entity_ids(query: str, scored: Iterable[_ScoredNode]) -> set[str]:
    """找出问句中出现实体名的已召回实体，作为受限结果的检索锚点。"""

    return {
        item.node.id
        for item in scored
        if item.node.type == DataLinkNodeType.ENTITY
        and _term_appears_in_query(item.node.name, query)
    }


def _add_direct_entity_context(
    context: dict[str, _ScoredNode],
    entity_ids: Iterable[str],
    scored: Mapping[str, _ScoredNode],
    node_by_id: Mapping[str, GraphNode],
    concepts_by_entity: Mapping[str, Sequence[GraphEdge]],
) -> None:
    """保留直接命中的实体及其 Concept，避免被解释节点预算截断。"""

    for entity_id in sorted(entity_ids):
        entity_score = scored.get(entity_id)
        if entity_score is None:
            continue
        entity = node_by_id.get(entity_id)
        if entity is None or entity.type != DataLinkNodeType.ENTITY:
            continue
        _add_scored_node(context, entity, entity_score.score)
        for edge in sorted(
            concepts_by_entity.get(entity_id, ()),
            key=_edge_rank,
        ):
            concept = node_by_id.get(edge.target_id)
            if concept is None or concept.type != DataLinkNodeType.CONCEPT:
                continue
            _add_scored_node(context, concept, _mapped_score(entity_score.score, edge.confidence))


def _physical_scored(items: Iterable[_ScoredNode]) -> tuple[_ScoredNode, ...]:
    """在预算截断前优先选择可直接写入 SQL 的字段和表。"""

    physical = (
        item
        for item in items
        if item.node.type in {DataLinkNodeType.COLUMN, DataLinkNodeType.TABLE}
    )
    return tuple(sorted(physical, key=lambda item: (-item.score, _node_sort_key(item.node))))


def _semantic_scored(
    items: Iterable[_ScoredNode], *, preferred_node_ids: set[str] | None = None
) -> tuple[_ScoredNode, ...]:
    """字段和表仍有预算时保留解释节点，直接命中的 Entity 优先。"""

    preferred = preferred_node_ids or set()
    semantic = (
        item
        for item in items
        if item.node.type in {DataLinkNodeType.CONCEPT, DataLinkNodeType.ENTITY}
    )
    return tuple(
        sorted(
            semantic,
            key=lambda item: (
                0 if item.node.id in preferred else 1,
                0 if item.node.type == DataLinkNodeType.CONCEPT else 1,
                -item.score,
                _node_sort_key(item.node),
            ),
        )
    )


def _node_sort_key(node: GraphNode) -> tuple[int, str, str]:
    """同分时保持字段、表、概念、实体的稳定且可解释顺序。"""

    order = {
        DataLinkNodeType.COLUMN: 0,
        DataLinkNodeType.TABLE: 1,
        DataLinkNodeType.CONCEPT: 2,
        DataLinkNodeType.ENTITY: 3,
    }
    return (order[node.type], node.name, node.id)


def _vector_node_weight(node_type: DataLinkNodeType) -> float:
    """向量召回优先物理节点，概念节点只作为字段展开的语义索引。"""

    if node_type in {DataLinkNodeType.COLUMN, DataLinkNodeType.TABLE}:
        return 1.2
    if node_type in {DataLinkNodeType.CONCEPT, DataLinkNodeType.ENTITY}:
        return 0.7
    return 1.0


def _edge_types_for_focus(focus: str | None) -> frozenset[DataLinkEdgeType]:
    """按焦点收紧邻居范围，join_paths 永远只扩展可连接关系。"""

    if focus == "join_paths":
        return _JOIN_TYPES
    if focus == "schema":
        return frozenset(
            {
                DataLinkEdgeType.CONTAINS,
                DataLinkEdgeType.REPRESENTS,
                DataLinkEdgeType.HAS_CONCEPT,
                *(_JOIN_TYPES),
            }
        )
    if focus == "data_profile":
        return frozenset({DataLinkEdgeType.CONTAINS})
    return frozenset(DataLinkEdgeType)


def _visible_edge_types_for_focus(focus: str | None) -> frozenset[DataLinkEdgeType]:
    """限制输出边类型，使 Join 模式不夹带可能被误用的语义说明边。"""

    return _JOIN_TYPES if focus == "join_paths" else frozenset(DataLinkEdgeType)


def _edge_read(edge: GraphEdge) -> DataLinkEdgeRead:
    """将内部边收敛为外部受限 DTO，省略所有内部属性。"""

    return DataLinkEdgeRead(
        source=edge.source_id,
        target=edge.target_id,
        type=edge.type,
        confidence=edge.confidence,
        evidence=edge.evidence,
        provenance=graph_provenance(edge),
        enabled=edge_enabled(edge),
    )


def _mapped_score(score: float, *confidences: float | None) -> float:
    """Manual mappings propagate lexical relevance without inventing confidence."""
    if any(value is None for value in confidences):
        return score
    return score * math.prod(confidences)


def _edge_rank(edge: GraphEdge) -> tuple:
    group = (
        2
        if edge.confidence is None or graph_provenance(edge) == "manual"
        else 0
        if edge.type == DataLinkEdgeType.FOREIGN_KEY
        else 1
    )
    score = () if group == 2 else (-edge.confidence,)
    return group, score, edge.source_id, edge.target_id, edge.id


def _path_rank(path: DataLinkJoinPathRead) -> tuple:
    return _path_rank_parts(path.steps, path.confidence)


def _path_rank_parts(steps: Sequence[DataLinkJoinPathStepRead], confidence: float | None) -> tuple:
    group = (
        2
        if confidence is None
        else 0
        if all(step.edge_type == DataLinkEdgeType.FOREIGN_KEY for step in steps)
        else 1
    )
    score = () if confidence is None else (-confidence,)
    endpoints = tuple(
        (s.source_table, s.source_column, s.target_table, s.target_column) for s in steps
    )
    return (group, endpoints) if confidence is None else (group, score, len(steps), endpoints)


def _table_name(node: GraphNode) -> str | None:
    """统一获取 Table 或 Column 节点所属表名。"""

    if node.type == DataLinkNodeType.TABLE:
        return node.name
    return node.table_name


def _column_table_pair(
    edge: GraphEdge, node_by_id: dict[str, GraphNode]
) -> tuple[GraphNode, GraphNode] | None:
    """只把两端均为字段且来自不同表的可连接边投影到表级 BFS。"""

    source = node_by_id.get(edge.source_id)
    target = node_by_id.get(edge.target_id)
    if (
        source is None
        or target is None
        or source.type != DataLinkNodeType.COLUMN
        or target.type != DataLinkNodeType.COLUMN
        or source.table_name is None
        or target.table_name is None
        or source.table_name == target.table_name
    ):
        return None
    return source, target


def _other_table(edge: GraphEdge, table_name: str, node_by_id: dict[str, GraphNode]) -> str | None:
    """取得一条 Join 边另一端的表名，供单个命中表寻找直接证据。"""

    pair = _column_table_pair(edge, node_by_id)
    if pair is None:
        return None
    source, target = pair
    if source.table_name == table_name:
        return target.table_name
    if target.table_name == table_name:
        return source.table_name
    return None


def _bfs_join_paths(
    source_table: str,
    target_table: str,
    edges: Sequence[GraphEdge],
    node_by_id: dict[str, GraphNode],
    *,
    max_paths: int,
) -> tuple[DataLinkJoinPathRead, ...]:
    """检索最多三跳的候选路径，分别保留统计候选和无分数人工候选。"""

    if max_paths < 1 or source_table == target_table:
        return ()
    scored_edges = tuple(
        edge for edge in edges if edge.confidence is not None and graph_provenance(edge) != "manual"
    )
    scored = _search_join_paths(
        source_table, target_table, scored_edges, node_by_id, max_paths=max_paths, manual_only=False
    )
    manual = (
        _search_join_paths(
            source_table, target_table, edges, node_by_id, max_paths=max_paths, manual_only=True
        )
        if len(scored_edges) != len(edges)
        else ()
    )
    return tuple(sorted((*scored, *manual), key=_path_rank)[:max_paths])


def _search_join_paths(
    source_table: str,
    target_table: str,
    edges: Sequence[GraphEdge],
    node_by_id: dict[str, GraphNode],
    *,
    max_paths: int,
    manual_only: bool,
) -> tuple[DataLinkJoinPathRead, ...]:
    adjacency: dict[str, list[tuple[str, DataLinkJoinPathStepRead]]] = {}
    for edge in sorted(edges, key=_edge_rank):
        pair = _column_table_pair(edge, node_by_id)
        if pair is None:
            continue
        source, target = pair
        forward = _join_step(source, target, edge)
        backward = _join_step(target, source, edge)
        adjacency.setdefault(source.table_name or "", []).append((target.table_name or "", forward))
        adjacency.setdefault(target.table_name or "", []).append(
            (source.table_name or "", backward)
        )

    serial = count()
    queue = [(_path_rank_parts((), 1.0), next(serial), source_table, (source_table,), (), 1.0)]
    found: list[DataLinkJoinPathRead] = []
    while queue and len(found) < max_paths:
        _, _, current, tables, steps, confidence = heappop(queue)
        if current == target_table and steps:
            if manual_only and confidence is not None:
                continue
            found.append(
                DataLinkJoinPathRead(
                    tables=list(tables),
                    steps=list(steps),
                    confidence=confidence,
                    evidence=_path_evidence(steps),
                    provenance="manual"
                    if any(step.provenance == "manual" for step in steps)
                    else "database_foreign_key"
                    if all(step.edge_type == DataLinkEdgeType.FOREIGN_KEY for step in steps)
                    else "unknown"
                    if any(step.provenance == "unknown" for step in steps)
                    else "inferred_candidate",
                )
            )
            continue
        if len(steps) >= _MAX_JOIN_HOPS:
            continue
        for next_table, step in adjacency.get(current, []):
            if next_table in tables:
                continue
            next_steps = (*steps, step)
            next_confidence = (
                None
                if confidence is None or step.confidence is None
                else confidence * step.confidence
            )
            heappush(
                queue,
                (
                    _path_rank_parts(next_steps, next_confidence),
                    next(serial),
                    next_table,
                    (*tables, next_table),
                    next_steps,
                    next_confidence,
                ),
            )
    return tuple(found)


def _join_step(source: GraphNode, target: GraphNode, edge: GraphEdge) -> DataLinkJoinPathStepRead:
    """把字段边转换为可直接解释的 Join 步骤，不携带样例值或内部属性。"""

    return DataLinkJoinPathStepRead(
        source_table=source.table_name or "",
        source_column=source.name,
        target_table=target.table_name or "",
        target_column=target.name,
        edge_type=edge.type,
        confidence=edge.confidence,
        evidence=edge.evidence,
        provenance=graph_provenance(edge),
    )


def _path_evidence(steps: Sequence[DataLinkJoinPathStepRead]) -> str:
    """给路径提供简短来源说明，明确区分显式外键和推断连接。"""

    if any(step.confidence is None or step.provenance == "manual" for step in steps):
        return "Path includes unverified manual candidates; statistical confidence is unavailable"
    if all(step.edge_type == DataLinkEdgeType.FOREIGN_KEY for step in steps):
        return "All steps use explicit foreign keys"
    return "Path includes inferred joinable relationships"


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算向量相似度；零向量或错误维度不会贡献召回结果。"""

    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_size = math.sqrt(sum(value * value for value in left))
    right_size = math.sqrt(sum(value * value for value in right))
    return numerator / (left_size * right_size) if left_size and right_size else 0.0
