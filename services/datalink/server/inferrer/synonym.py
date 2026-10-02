"""根据字段名、同义词组和已知语义类型推断说明关系。"""

from __future__ import annotations

from itertools import combinations

from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType

from server.extractor.tabular import make_graph_id
from server.models.graph import GraphEdge, GraphNode
from server.models.profile import ColumnProfile

_SYNONYM_GROUPS = (
    frozenset({"id", "identifier", "uid", "uuid", "guid"}),
    frozenset({"customer_id", "user_id", "client_id", "account_id", "person_id"}),
    frozenset({"name", "full_name", "person_name", "display_name"}),
    frozenset({"email", "email_address", "mail"}),
    frozenset({"phone", "phone_number", "telephone", "tel", "mobile"}),
    frozenset({"address", "street_address", "location", "physical_address"}),
    frozenset({"amount", "value", "total", "sum", "price", "cost", "fee", "charge"}),
    frozenset({"date", "timestamp", "created_at", "updated_at", "time", "datetime"}),
    frozenset({"status", "state", "condition", "phase"}),
    frozenset({"type", "category", "class", "kind", "group"}),
    frozenset({"city", "city_name", "town", "municipality"}),
    frozenset({"country", "country_name", "nation", "region"}),
    frozenset({"age", "years", "year_old"}),
    frozenset({"gender", "sex"}),
    frozenset({"latitude", "lat", "y"}),
    frozenset({"longitude", "lng", "lon", "x"}),
    frozenset({"description", "desc", "notes", "comment", "details", "remarks"}),
)


class SynonymInferrer:
    """用可解释的字段名规则生成跨表语义同义关系。"""

    def infer(
        self, nodes: tuple[GraphNode, ...], profiles: tuple[ColumnProfile, ...]
    ) -> tuple[GraphEdge, ...]:
        """比较不同表的字段，只生成带评分和证据的说明边。"""

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
            if left_profile.is_sensitive or right_profile.is_sensitive:
                continue
            confidence = _synonym_confidence(left, right, left_profile, right_profile)
            if confidence <= 0:
                continue
            edges.append(
                GraphEdge(
                    id=make_graph_id("edge", "semantic_synonym", left.id, right.id),
                    source_id=left.id,
                    target_id=right.id,
                    type=DataLinkEdgeType.SEMANTIC_SYNONYM,
                    confidence=confidence,
                    evidence=DataLinkEdgeEvidenceRead(
                        kind="semantic_similarity",
                        summary=f"Column names or semantic types match at {confidence:.2f}",
                    ),
                    properties={
                        "name_similarity": _name_similarity(left.name, right.name),
                        "synonym_group_match": _same_synonym_group(left.name, right.name),
                        "semantic_type_match": _same_known_semantic_type(
                            left, right, left_profile, right_profile
                        ),
                    },
                )
            )
        return tuple(edges)


def _synonym_confidence(
    left: GraphNode,
    right: GraphNode,
    left_profile: ColumnProfile,
    right_profile: ColumnProfile,
) -> float:
    """按 DataFoundry 的三类证据合成固定置信度。"""

    type_match = _same_known_semantic_type(left, right, left_profile, right_profile)
    name_similarity = _name_similarity(left.name, right.name)
    group_match = _same_synonym_group(left.name, right.name)
    if type_match and (name_similarity > 0.5 or group_match):
        return 0.95
    if type_match:
        return 0.85
    if group_match:
        return 0.80
    if name_similarity > 0.7:
        return 0.60
    if name_similarity > 0.5:
        return 0.40
    return 0.0


def _same_known_semantic_type(
    left: GraphNode,
    right: GraphNode,
    left_profile: ColumnProfile,
    right_profile: ColumnProfile,
) -> bool:
    """只把两个明确且相同的语义类型当成类型证据。"""

    left_type = left.semantic_type or left_profile.semantic_type
    right_type = right.semantic_type or right_profile.semantic_type
    return bool(left_type and right_type and left_type != "unknown" and left_type == right_type)


def _normalize_name(value: str) -> str:
    """统一字段名大小写和分隔符，保持同义词规则可重复。"""

    return value.casefold().replace("-", "_").replace(" ", "_")


def _same_synonym_group(left: str, right: str) -> bool:
    """判断两个字段名是否落在同一个固定同义词组中。"""

    left_name = _normalize_name(left)
    right_name = _normalize_name(right)
    return any(left_name in group and right_name in group for group in _SYNONYM_GROUPS)


def _name_similarity(left: str, right: str) -> float:
    """用子串、字符集合和公共前缀计算无需模型的名称相似度。"""

    left_name = _normalize_name(left).replace("_", "")
    right_name = _normalize_name(right).replace("_", "")
    if left_name == right_name:
        return 1.0
    if not left_name or not right_name:
        return 0.0
    if left_name in right_name or right_name in left_name:
        ratio = min(len(left_name), len(right_name)) / max(len(left_name), len(right_name))
        return 0.5 + 0.5 * ratio
    left_chars = set(left_name)
    right_chars = set(right_name)
    jaccard = len(left_chars & right_chars) / len(left_chars | right_chars)
    prefix_length = 0
    for left_char, right_char in zip(left_name, right_name, strict=False):
        if left_char != right_char:
            break
        prefix_length += 1
    prefix_score = prefix_length / max(len(left_name), len(right_name))
    return max(jaccard, prefix_score)
