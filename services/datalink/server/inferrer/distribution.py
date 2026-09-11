"""根据脱敏画像推断跨表字段的分布相似说明关系。"""

from __future__ import annotations

from itertools import combinations

from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType

from server.extractor.tabular import make_graph_id
from server.models.graph import GraphEdge, GraphNode
from server.models.profile import ColumnProfile

_NUMERIC_TYPES = frozenset({"integer", "float"})
_TEMPORAL_TYPES = frozenset({"date", "datetime"})


class DistributionInferrer:
    """只用已脱敏的范围和常见值，生成可解释的分布相似关系。"""

    def __init__(self, similarity_threshold: float = 0.5) -> None:
        self.similarity_threshold = similarity_threshold

    def infer(
        self, nodes: tuple[GraphNode, ...], profiles: tuple[ColumnProfile, ...]
    ) -> tuple[GraphEdge, ...]:
        """比较不同表且声明类型相同的非敏感字段。"""

        profile_by_column = {profile.column_id: profile for profile in profiles}
        columns = tuple(node for node in nodes if node.type == DataLinkNodeType.COLUMN)
        edges: list[GraphEdge] = []
        for left, right in combinations(columns, 2):
            if left.table_name is None or left.table_name == right.table_name:
                continue
            left_profile = profile_by_column.get(left.id)
            right_profile = profile_by_column.get(right.id)
            if left_profile is None or right_profile is None:
                continue
            if (
                left_profile.is_sensitive
                or right_profile.is_sensitive
                or left_profile.dtype != right_profile.dtype
            ):
                continue
            similarity = _distribution_similarity(left_profile, right_profile)
            if similarity < self.similarity_threshold:
                continue
            edges.append(
                GraphEdge(
                    id=make_graph_id("edge", "distribution_similar", left.id, right.id),
                    source_id=left.id,
                    target_id=right.id,
                    type=DataLinkEdgeType.DISTRIBUTION_SIMILAR,
                    confidence=similarity,
                    evidence=DataLinkEdgeEvidenceRead(
                        kind="profile_distribution",
                        summary=f"Limited profile distributions match at {similarity:.2f}",
                    ),
                    properties={"similarity_score": similarity, "dtype": left_profile.dtype},
                )
            )
        return tuple(edges)


def _distribution_similarity(left: ColumnProfile, right: ColumnProfile) -> float:
    """按类型选择范围或常见值重合度，无法比较时返回零。"""

    if left.dtype in _NUMERIC_TYPES:
        return _numeric_similarity(left, right)
    if left.dtype == "text" or left.dtype in _TEMPORAL_TYPES:
        return _value_similarity(left, right)
    return 0.0


def _numeric_similarity(left: ColumnProfile, right: ColumnProfile) -> float:
    """以两个脱敏数值范围的重叠比例近似分布相似度。"""

    if left.min_value is None or left.max_value is None:
        return 0.0
    if right.min_value is None or right.max_value is None:
        return 0.0
    try:
        left_min, left_max = float(left.min_value), float(left.max_value)
        right_min, right_max = float(right.min_value), float(right.max_value)
    except (TypeError, ValueError):
        return 0.0
    overlap_start = max(left_min, right_min)
    overlap_end = min(left_max, right_max)
    if overlap_start > overlap_end:
        return 0.0
    total_range = max(left_max, right_max) - min(left_min, right_min)
    return 1.0 if total_range == 0 else (overlap_end - overlap_start) / total_range


def _value_similarity(left: ColumnProfile, right: ColumnProfile) -> float:
    """用有限常见值集合的 Jaccard 比例近似分类或时间分布。"""

    left_values = {_comparison_token(value) for value in left.top_values}
    right_values = {_comparison_token(value) for value in right.top_values}
    if not left_values or not right_values:
        return 0.0
    return len(left_values & right_values) / len(left_values | right_values)


def _comparison_token(value: object) -> str:
    """避免大小写差异让相同的有限画像值失去匹配。"""

    return str(value).casefold()
