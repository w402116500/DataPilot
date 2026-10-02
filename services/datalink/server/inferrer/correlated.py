"""按 Joinable 对齐样本后推断非敏感数值字段的相关说明关系。"""

from __future__ import annotations

from itertools import product
from math import sqrt

from contracts.datalink import DataLinkEdgeType, DataLinkNodeType

from server.connector.base import ReadOnlyConnector, SourceRow
from server.extractor.tabular import make_graph_id
from server.models.graph import GraphEdge, GraphNode
from server.models.profile import ColumnProfile

_NUMERIC_TYPES = frozenset({"integer", "float"})
MIN_ALIGNED_ROWS = 5


class CorrelationInferrer:
    """沿用 DataFoundry 的规则，只用 joinable 关系作为表级对齐键。"""

    def __init__(self, correlation_threshold: float = 0.5) -> None:
        self.correlation_threshold = correlation_threshold

    def infer(
        self,
        nodes: tuple[GraphNode, ...],
        profiles: tuple[ColumnProfile, ...],
        joinable_edges: tuple[GraphEdge, ...],
        connector: ReadOnlyConnector,
    ) -> tuple[GraphEdge, ...]:
        """连接每个 Joinable 键的有限样本，再计算跨表数值字段 Pearson 相关。"""

        profile_by_column = {profile.column_id: profile for profile in profiles}
        column_by_id = {node.id: node for node in nodes if node.type == DataLinkNodeType.COLUMN}
        rows_by_table: dict[str, list[SourceRow]] = {}
        edges_by_pair: dict[tuple[str, str], GraphEdge] = {}
        for joinable in joinable_edges:
            if joinable.type != DataLinkEdgeType.JOINABLE:
                continue
            left = column_by_id.get(joinable.source_id)
            right = column_by_id.get(joinable.target_id)
            if left is None or right is None:
                continue
            if left.table_name is None or right.table_name is None:
                continue
            left_profile = profile_by_column.get(left.id)
            right_profile = profile_by_column.get(right.id)
            if left_profile is None or right_profile is None:
                continue
            if left_profile.is_sensitive or right_profile.is_sensitive:
                continue
            left_rows = rows_by_table.setdefault(
                left.table_name, connector.sample_rows(left.table_name)
            )
            right_rows = rows_by_table.setdefault(
                right.table_name, connector.sample_rows(right.table_name)
            )
            aligned = _align_rows(left_rows, right_rows, left.name, right.name)
            if len(aligned) < MIN_ALIGNED_ROWS:
                continue
            left_columns = _numeric_columns(nodes, profile_by_column, left.table_name, left.name)
            right_columns = _numeric_columns(nodes, profile_by_column, right.table_name, right.name)
            for left_column, right_column in product(left_columns, right_columns):
                values = [
                    (left_row.get(left_column.name), right_row.get(right_column.name))
                    for left_row, right_row in aligned
                ]
                coefficient = _pearson(values)
                if coefficient is None or abs(coefficient) < self.correlation_threshold:
                    continue
                source_id, target_id = sorted((left_column.id, right_column.id))
                edge = GraphEdge(
                    id=make_graph_id("edge", "correlated", source_id, target_id),
                    source_id=source_id,
                    target_id=target_id,
                    type=DataLinkEdgeType.CORRELATED,
                    confidence=abs(coefficient),
                    properties={
                        "coefficient": coefficient,
                        "method": "pearson",
                        "aligned_rows": len(aligned),
                        "joinable_edge": joinable.id,
                        "join_key_source": left.id,
                        "join_key_target": right.id,
                    },
                )
                existing = edges_by_pair.get((source_id, target_id))
                if existing is None or edge.confidence > existing.confidence:
                    edges_by_pair[(source_id, target_id)] = edge
        return tuple(edges_by_pair.values())


def _numeric_columns(
    nodes: tuple[GraphNode, ...],
    profile_by_column: dict[str, ColumnProfile],
    table_name: str,
    excluded_name: str,
) -> tuple[GraphNode, ...]:
    """筛出同表、非键、非敏感的数值字段。"""

    return tuple(
        node
        for node in nodes
        if node.type == DataLinkNodeType.COLUMN
        and node.table_name == table_name
        and node.name != excluded_name
        and (profile := profile_by_column.get(node.id)) is not None
        and profile.dtype in _NUMERIC_TYPES
        and not profile.is_sensitive
    )


def _align_rows(
    left_rows: list[SourceRow],
    right_rows: list[SourceRow],
    left_key: str,
    right_key: str,
) -> tuple[tuple[SourceRow, SourceRow], ...]:
    """按 Joinable 键对齐有限行，保留一对多连接产生的配对。"""

    right_by_key: dict[str, list[SourceRow]] = {}
    for row in right_rows:
        key = _comparison_token(row.get(right_key))
        if key is not None:
            right_by_key.setdefault(key, []).append(row)
    aligned: list[tuple[SourceRow, SourceRow]] = []
    for left_row in left_rows:
        key = _comparison_token(left_row.get(left_key))
        if key is None:
            continue
        aligned.extend((left_row, right_row) for right_row in right_by_key.get(key, ()))
    return tuple(aligned)


def _comparison_token(value: object) -> str | None:
    """让数值和文本标识符可按 DataFoundry 规则进行有限对齐。"""

    if value is None:
        return None
    return str(value)


def _pearson(values: list[tuple[object, object]]) -> float | None:
    """计算成对数值的 Pearson 系数，异常值和非数值样本直接跳过。"""

    pairs: list[tuple[float, float]] = []
    for left, right in values:
        try:
            pairs.append((float(left), float(right)))
        except (TypeError, ValueError):
            continue
    if len(pairs) < MIN_ALIGNED_ROWS:
        return None
    left_mean = sum(left for left, _ in pairs) / len(pairs)
    right_mean = sum(right for _, right in pairs) / len(pairs)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_variance = sum((left - left_mean) ** 2 for left, _ in pairs)
    right_variance = sum((right - right_mean) ** 2 for _, right in pairs)
    denominator = sqrt(left_variance * right_variance)
    return None if denominator == 0 else numerator / denominator
