"""按 DataFoundry 两步思路整理跨批同义 Concept 和 Entity。"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations, product

from contracts.datalink import DataLinkEdgeType, DataLinkNodeType

from server.extractor.tabular import make_graph_id
from server.mapper.client import (
    ModelConfigurationError,
    ModelResponseError,
    SemanticModelClient,
    should_keep_embedding_candidate,
)
from server.mapper.schemas import MergeCandidate, MergeDecision, SemanticNodeInput
from server.models.graph import GraphEdge, GraphNode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SemanticMergeResult:
    """一次同义整理后的节点与重定向、去重后的关系。"""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


class SemanticMerger:
    """同名或 alias 先确定性吸收，其余再 Embedding 预筛并由模型确认；判断失败保留未合并节点。"""

    def __init__(self, client: SemanticModelClient) -> None:
        self.client = client

    def merge(
        self,
        new_nodes: Sequence[GraphNode],
        new_edges: Sequence[GraphEdge],
        existing_nodes: Sequence[GraphNode],
        existing_edges: Sequence[GraphEdge],
    ) -> SemanticMergeResult:
        """把新一批语义节点纳入已有结果；合并判断问题不能破坏完整字段映射。"""

        candidates = self._candidates(new_nodes, existing_nodes)
        all_nodes = tuple(existing_nodes) + tuple(new_nodes)
        all_edges = tuple(existing_edges) + tuple(new_edges)
        deterministic = _deterministic_replacements(candidates, all_nodes)
        if deterministic:
            all_nodes = _merge_nodes(all_nodes, deterministic)
            all_edges = _deduplicate_edges(all_edges, deterministic)
            absorbed = set(deterministic)
            candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.new_id not in absorbed and candidate.existing_id not in absorbed
            )
        if not candidates:
            return SemanticMergeResult(nodes=all_nodes, edges=_deduplicate_edges(all_edges, {}))

        remaining_new, remaining_existing = _surviving_merge_inputs(
            new_nodes, existing_nodes, all_nodes
        )
        try:
            prefiltered_candidates = self._embedding_prefilter(candidates, all_nodes)
            selected_candidates = (
                candidates
                if prefiltered_candidates is None or not prefiltered_candidates
                else prefiltered_candidates
            )
            response = self.client.judge_merges(
                _to_model_nodes(remaining_new),
                _to_model_nodes(remaining_existing),
                selected_candidates,
            )
            replacements = _validated_replacements(response.merges, selected_candidates)
        except (ModelConfigurationError, ModelResponseError, ValueError):
            logger.warning("Semantic merge judgment failed; keeping duplicate nodes")
            return SemanticMergeResult(nodes=all_nodes, edges=_deduplicate_edges(all_edges, {}))

        return SemanticMergeResult(
            nodes=_merge_nodes(all_nodes, replacements),
            edges=_deduplicate_edges(all_edges, replacements),
        )

    def _candidates(
        self, new_nodes: Sequence[GraphNode], existing_nodes: Sequence[GraphNode]
    ) -> tuple[MergeCandidate, ...]:
        """只比较同类型语义节点；第一轮对新节点内部做不带偏见的比较。"""

        candidates: list[MergeCandidate] = []
        if existing_nodes:
            pairs = product(new_nodes, existing_nodes)
        else:
            pairs = combinations(new_nodes, 2)
        for new_node, existing_node in pairs:
            if new_node.type != existing_node.type or new_node.type not in {
                DataLinkNodeType.CONCEPT,
                DataLinkNodeType.ENTITY,
            }:
                continue
            candidates.append(
                MergeCandidate(
                    new_id=new_node.id,
                    existing_id=existing_node.id,
                    type=new_node.type.value,
                )
            )
        return tuple(candidates)

    def _embedding_prefilter(
        self, candidates: Sequence[MergeCandidate], nodes: Sequence[GraphNode]
    ) -> tuple[MergeCandidate, ...] | None:
        """Embedding 可用时预筛候选；任何不可用情况都交还给模型全量比较。"""

        node_by_id = {node.id: node for node in nodes}
        candidate_ids = tuple(
            dict.fromkeys(
                node_id
                for candidate in candidates
                for node_id in (candidate.new_id, candidate.existing_id)
            )
        )
        try:
            vectors = self.client.embed(
                [_embedding_text(node_by_id[node_id]) for node_id in candidate_ids]
            )
        except (ModelConfigurationError, ModelResponseError, ValueError, TypeError):
            # Embedding 只是预筛；不可用时必须交回模型做完整候选判断。
            return None
        if vectors is None:
            return None
        vector_by_id = dict(zip(candidate_ids, vectors, strict=True))
        return tuple(
            candidate
            for candidate in candidates
            if should_keep_embedding_candidate(
                _cosine_similarity(
                    vector_by_id[candidate.new_id], vector_by_id[candidate.existing_id]
                )
            )
        )


def _same_name_or_alias(left: GraphNode, right: GraphNode) -> bool:
    """主名或对方别名经 casefold 命中即视为同一节点；不看外键或 Join 边。"""

    left_name = left.name.casefold()
    right_name = right.name.casefold()
    if left_name == right_name:
        return True
    left_aliases = {alias.casefold() for alias in left.aliases}
    right_aliases = {alias.casefold() for alias in right.aliases}
    return left_name in right_aliases or right_name in left_aliases


def _deterministic_replacements(
    candidates: Sequence[MergeCandidate],
    nodes: Sequence[GraphNode],
) -> dict[str, str]:
    """把同名或 alias 命中的候选收成一组，保留序列中最先出现的节点。"""

    node_by_id = {node.id: node for node in nodes}
    order = {node.id: index for index, node in enumerate(nodes)}
    parent = {node.id: node.id for node in nodes}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    matched = False
    for candidate in candidates:
        left = node_by_id[candidate.new_id]
        right = node_by_id[candidate.existing_id]
        if not _same_name_or_alias(left, right):
            continue
        matched = True
        left_root = find(left.id)
        right_root = find(right.id)
        if left_root == right_root:
            continue
        if order[left_root] <= order[right_root]:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    if not matched:
        return {}
    return {node.id: find(node.id) for node in nodes if find(node.id) != node.id}


def _surviving_merge_inputs(
    new_nodes: Sequence[GraphNode],
    existing_nodes: Sequence[GraphNode],
    merged_nodes: Sequence[GraphNode],
) -> tuple[tuple[GraphNode, ...], tuple[GraphNode, ...]]:
    """模型只比较吸收后仍在的节点，并带上已合并的别名。"""

    merged_by_id = {node.id: node for node in merged_nodes}
    surviving_new = tuple(merged_by_id[node.id] for node in new_nodes if node.id in merged_by_id)
    surviving_existing = tuple(
        merged_by_id[node.id] for node in existing_nodes if node.id in merged_by_id
    )
    return surviving_new, surviving_existing


def _to_model_nodes(nodes: Sequence[GraphNode]) -> tuple[SemanticNodeInput, ...]:
    """缩小同义判断输入，只发送名称、说明和别名而不发送图谱关系或原始值。"""

    return tuple(
        SemanticNodeInput(
            id=node.id,
            type=node.type.value,
            name=node.name,
            description=node.description,
            aliases=list(node.aliases),
        )
        for node in nodes
        if node.type in {DataLinkNodeType.CONCEPT, DataLinkNodeType.ENTITY}
    )


def _embedding_text(node: GraphNode) -> str:
    """为语义节点构造有限文本，避免向量预筛读取字段画像或原始数据。"""

    return "\n".join(part for part in (node.name, node.description, *node.aliases) if part)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算有限向量的余弦相似度；异常维度或零向量不产生预筛候选。"""

    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_size = math.sqrt(sum(value * value for value in left))
    right_size = math.sqrt(sum(value * value for value in right))
    return numerator / (left_size * right_size) if left_size and right_size else 0.0


def _validated_replacements(
    decisions: Sequence[MergeDecision], candidates: Sequence[MergeCandidate]
) -> dict[str, str]:
    """只接受模型明确确认的候选对，并拒绝一节点多目标的合并计划。"""

    allowed = {(candidate.new_id, candidate.existing_id) for candidate in candidates}
    replacements: dict[str, str] = {}
    for decision in decisions:
        new_id = decision.new_id
        existing_id = decision.existing_id
        if (new_id, existing_id) not in allowed or new_id in replacements:
            raise ValueError("Model merge plan is invalid")
        replacements[new_id] = existing_id
    if any(target_id in replacements for target_id in replacements.values()):
        raise ValueError("Model merge plan must not contain chained replacements")
    return replacements


def _merge_nodes(nodes: Sequence[GraphNode], replacements: dict[str, str]) -> tuple[GraphNode, ...]:
    """保留目标节点，吸收被合并节点的别名和可用说明。"""

    node_by_id = {node.id: node for node in nodes}
    if any(target_id not in node_by_id for target_id in replacements.values()):
        raise ValueError("Model merge target is unavailable")
    absorbed: dict[str, list[GraphNode]] = {}
    for source_id, target_id in replacements.items():
        source = node_by_id.get(source_id)
        if source is None or source.type != node_by_id[target_id].type:
            raise ValueError("Model merge source is invalid")
        absorbed.setdefault(target_id, []).append(source)

    merged: list[GraphNode] = []
    for node in nodes:
        if node.id in replacements:
            continue
        matching = absorbed.get(node.id, [])
        if not matching:
            merged.append(node)
            continue
        aliases = tuple(
            dict.fromkeys(
                alias
                for source in matching
                for alias in (*source.aliases, source.name)
                if alias != node.name
            )
        )
        merged.append(
            GraphNode(
                id=node.id,
                type=node.type,
                name=node.name,
                table_name=node.table_name,
                semantic_type=node.semantic_type,
                description=node.description
                or next((source.description for source in matching if source.description), None),
                aliases=tuple(dict.fromkeys((*node.aliases, *aliases))),
                properties=node.properties,
            )
        )
    return tuple(merged)


def _deduplicate_edges(
    edges: Sequence[GraphEdge], replacements: dict[str, str]
) -> tuple[GraphEdge, ...]:
    """重定向合并边并按端点和类型去重，保留置信度更高的那条。"""

    selected: dict[tuple[str, str, DataLinkEdgeType], GraphEdge] = {}
    for edge in edges:
        source_id = replacements.get(edge.source_id, edge.source_id)
        target_id = replacements.get(edge.target_id, edge.target_id)
        redirected = GraphEdge(
            id=make_graph_id("edge", edge.type.value, source_id, target_id),
            source_id=source_id,
            target_id=target_id,
            type=edge.type,
            confidence=edge.confidence,
            evidence=edge.evidence,
            properties=edge.properties,
        )
        key = (source_id, target_id, edge.type)
        existing = selected.get(key)
        if existing is None or redirected.confidence > existing.confidence:
            selected[key] = redirected
    return tuple(selected.values())
