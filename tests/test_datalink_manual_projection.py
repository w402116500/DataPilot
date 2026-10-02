"""Null scores and human provenance survive the single safe model projection."""

from agent_runtime.contracts import DataLinkExploreResponse
from agent_runtime.datalink_semantics import (
    project_datalink_semantic_context,
    slim_datalink_semantic_context_for_model,
    strip_sensitive_typical_values,
)
from contracts.datalink import (
    DataLinkColumnProfileRead,
    DataLinkExploreResult,
    DataLinkNodeRead,
    DataLinkNodeType,
)
from contracts.sensitive_fields import SensitiveFieldPolicy


def test_manual_mapping_relation_and_path_survive_safe_projection():
    step = dict(
        source_table="orders",
        source_column="customer_id",
        target_table="customers",
        target_column="id",
        edge_type="joinable",
        confidence=None,
        provenance="manual",
    )
    result = DataLinkExploreResult(
        datasource_id="source",
        graph_version="version",
        query="customer",
        nodes=[
            dict(id="a", type="column", table="orders", name="customer_id", provenance="manual"),
            dict(id="b", type="column", table="customers", name="id"),
            dict(id="c", type="concept", name="Customer identifier", provenance="manual"),
            dict(id="e", type="entity", name="Customer", provenance="manual"),
        ],
        edges=[
            dict(source="a", target="c", type="represents", confidence=None, provenance="manual"),
            dict(source="e", target="c", type="has_concept", confidence=None, provenance="manual"),
            dict(source="a", target="b", type="joinable", confidence=None, provenance="manual"),
            dict(source="a", target="b", type="foreign_key", confidence=1, enabled=False),
        ],
        join_paths=[
            dict(
                tables=["orders", "customers"],
                steps=[step],
                confidence=None,
                provenance="manual",
                evidence="Unverified manual relationship",
            )
        ],
    )
    context = project_datalink_semantic_context(
        DataLinkExploreResponse(result=result, cache_hit=False)
    )
    mapping = next(field for field in context.fields if field.table == "orders").semantic_mappings[
        0
    ]
    assert mapping.field_to_concept_confidence is None and mapping.provenance == "manual"
    assert mapping.entities[0].entity_to_concept_confidence is None
    assert mapping.entities[0].provenance == "manual"
    assert len(context.relationships) == 1 and context.relationships[0].confidence is None
    assert context.relationships[0].provenance == "manual"
    assert context.join_paths[0].confidence is None and context.join_paths[0].provenance == "manual"
    assert context.join_paths[0].steps[0].provenance == "manual"
    assert '"id":' not in context.model_dump_json()


def _column_node(
    node_id: str,
    table: str,
    name: str,
    typical_values: list[str],
) -> dict:
    return {
        "id": node_id,
        "type": "column",
        "table": table,
        "name": name,
        "profile": {
            "column_id": node_id,
            "dtype": "text",
            "null_rate": 0,
            "distinct_count": len(typical_values),
            "unique_rate": 1,
            "typical_values": typical_values,
        },
    }


def _explore_with_columns(columns: list[dict]) -> DataLinkExploreResult:
    return DataLinkExploreResult(
        datasource_id="source",
        graph_version="version",
        query="orders",
        nodes=columns,
    )


def test_projection_carries_typical_values_from_column_profiles():
    result = _explore_with_columns(
        [
            _column_node("a", "orders", "status", ["paid", "pending", "canceled"]),
            _column_node("b", "customers", "channel", ["direct"]),
        ]
    )
    context = project_datalink_semantic_context(
        DataLinkExploreResponse(result=result, cache_hit=False)
    )
    by_table = {field.table: field for field in context.fields}
    assert by_table["orders"].typical_values == ["paid", "pending", "canceled"]
    assert by_table["customers"].typical_values == ["direct"]
    assert context.model_dump_json().count("canceled") == 1


def test_projection_trims_typical_values_deterministically_within_total_budget(monkeypatch):
    import agent_runtime.datalink_semantics as datalink_semantics

    monkeypatch.setattr(datalink_semantics, "TYPICAL_VALUES_MAX_TOTAL_CHARS", 30)
    result = _explore_with_columns(
        [
            _column_node("a", "orders", "status", ["paid", "pending", "canceled"]),
            _column_node("b", "orders", "channel", ["direct", "partner"]),
        ]
    )

    context = project_datalink_semantic_context(
        DataLinkExploreResponse(result=result, cache_hit=False)
    )

    # 投影顺序按 (table, column) 稳定排序；预算耗尽后其余字段整体置空。
    assert context.fields[0].column == "channel"
    assert context.fields[0].typical_values == ["direct", "partner"]
    assert context.fields[1].column == "status"
    assert context.fields[1].typical_values == []
    again = project_datalink_semantic_context(
        DataLinkExploreResponse(result=result, cache_hit=False)
    )
    assert again.fields[0].typical_values == context.fields[0].typical_values
    assert again.fields[1].typical_values == context.fields[1].typical_values


def test_projection_truncates_single_typical_value_to_contract_limit():
    # 契约层会拒绝超长值；这里用 model_construct 模拟绕过校验的旧数据源，
    # 验证投影仍然兜底截断到 200 字符。
    profile = DataLinkColumnProfileRead.model_construct(
        column_id="a",
        dtype="text",
        null_rate=0,
        distinct_count=1,
        unique_rate=1,
        typical_values=["x" * 500],
    )
    node = DataLinkNodeRead.model_construct(
        id="a",
        type=DataLinkNodeType.COLUMN,
        table="orders",
        name="status",
        profile=profile,
    )
    result = DataLinkExploreResult.model_construct(
        datasource_id="source",
        graph_version="version",
        query="orders",
        nodes=[node],
        edges=[],
        join_paths=[],
        warnings=[],
    )

    context = project_datalink_semantic_context(
        DataLinkExploreResponse(result=result, cache_hit=False)
    )

    assert context.fields[0].typical_values == ["x" * 200]


def test_slim_semantic_context_keeps_typical_values():
    result = _explore_with_columns([_column_node("a", "orders", "status", ["paid", "pending"])])
    context = project_datalink_semantic_context(
        DataLinkExploreResponse(result=result, cache_hit=False)
    )

    slimmed = slim_datalink_semantic_context_for_model(context)

    assert slimmed["fields"][0]["typical_values"] == ["paid", "pending"]
    # 历史已落盘的 slim 投影缺新键时照常瘦身，不因缺键失败。
    legacy = context.model_dump(mode="json")
    for field in legacy["fields"]:
        field.pop("typical_values")
    reslimmed = slim_datalink_semantic_context_for_model(legacy)
    assert reslimmed["fields"][0]["typical_values"] == []


def test_strip_sensitive_typical_values_removes_only_masked_columns():
    policy = SensitiveFieldPolicy(mask_fields=frozenset({"email"}), confirmed=True)
    result = _explore_with_columns(
        [
            _column_node("a", "orders", "email", ["a@example.test", "b@example.test"]),
            _column_node("b", "orders", "status", ["paid", "pending"]),
        ]
    )
    response = DataLinkExploreResponse(result=result, cache_hit=False)

    sanitized = strip_sensitive_typical_values(response, policy)

    nodes = {node.name: node for node in sanitized.result.nodes}
    assert nodes["email"].profile is not None
    assert nodes["email"].profile.typical_values == []
    assert nodes["email"].profile.distinct_count == 2
    assert nodes["status"].profile is not None
    assert nodes["status"].profile.typical_values == ["paid", "pending"]
    # 原响应不被就地修改；三处消费（plan 投影 / 循环投影 / 消费记录）基于
    # 同一个脱敏响应，所见即所得。
    original_nodes = {node.name: node for node in response.result.nodes}
    assert original_nodes["email"].profile is not None
    assert original_nodes["email"].profile.typical_values == [
        "a@example.test",
        "b@example.test",
    ]
