"""根据受限字段样本的重合度推断 Joinable 候选关系。"""

from __future__ import annotations

from itertools import combinations

from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType

from server.extractor.tabular import make_graph_id
from server.models.graph import GraphEdge, GraphNode
from server.models.profile import ColumnProfile

_NUMERIC_TYPES = frozenset({"integer", "float"})
_TEXT_TYPES = frozenset({"text"})
_TEMPORAL_TYPES = frozenset({"date", "datetime"})


class JoinableInferrer:
    """仅用非敏感字段的有限值重合证据，寻找同数据源跨表 Join 候选。"""

    def __init__(self, overlap_threshold: float = 0.1, max_cardinality: int = 900) -> None:
        self.overlap_threshold = overlap_threshold
        self.max_cardinality = max_cardinality

    def infer(
        self, nodes: tuple[GraphNode, ...], profiles: tuple[ColumnProfile, ...]
    ) -> tuple[GraphEdge, ...]:
        """返回跨表、类型兼容且值重合达到阈值的 Joinable 边。"""

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
            if not self._is_candidate(left_profile) or not self._is_candidate(right_profile):
                continue
            if not _compatible_dtypes(left_profile.dtype, right_profile.dtype):
                continue
            overlap_rate = _overlap_rate(left_profile, right_profile)
            if overlap_rate < self.overlap_threshold:
                continue
            edges.append(
                GraphEdge(
                    id=make_graph_id("edge", "joinable", left.id, right.id),
                    source_id=left.id,
                    target_id=right.id,
                    type=DataLinkEdgeType.JOINABLE,
                    confidence=overlap_rate,
                    evidence=DataLinkEdgeEvidenceRead(
                        kind="value_overlap",
                        summary=f"Limited value samples overlap at {overlap_rate:.2f}",
                    ),
                    properties={
                        "overlap_rate": overlap_rate,
                        "source_dtype": left_profile.dtype,
                        "target_dtype": right_profile.dtype,
                    },
                )
            )
        return tuple(edges)

    def _is_candidate(self, profile: ColumnProfile) -> bool:
        """过滤敏感、布尔和高基数字段，避免无意义或不安全的候选关系。"""

        return (
            not profile.is_sensitive
            and profile.dtype != "boolean"
            and profile.distinct_count <= self.max_cardinality
        )


def _compatible_dtypes(left: str, right: str) -> bool:
    """允许同类类型及数值/文本标识符比较，其余组合不进入重合计算。"""

    if left in _NUMERIC_TYPES and right in _NUMERIC_TYPES:
        return True
    if left in _TEXT_TYPES and right in _TEXT_TYPES:
        return True
    if left in _TEMPORAL_TYPES and right in _TEMPORAL_TYPES:
        return True
    return (left in _NUMERIC_TYPES and right in _TEXT_TYPES) or (
        left in _TEXT_TYPES and right in _NUMERIC_TYPES
    )


def _overlap_rate(left: ColumnProfile, right: ColumnProfile) -> float:
    """以常见值和有限样例估算较小取值域中有多少值能够对上。"""

    left_values = {_comparison_token(value) for value in (*left.top_values, *left.sample_values)}
    right_values = {_comparison_token(value) for value in (*right.top_values, *right.sample_values)}
    if not left_values or not right_values:
        return 0.0
    return len(left_values & right_values) / min(len(left_values), len(right_values))


def _comparison_token(value: object) -> str:
    """让数值 1 与文本 '1' 能作为标识符候选匹配，同时避免大小写造成漂移。"""

    return str(value).casefold()
