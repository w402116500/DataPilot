"""Null scores and human provenance survive the single safe model projection."""

from agent_runtime.contracts import DataLinkExploreResponse
from agent_runtime.datalink_semantics import project_datalink_semantic_context
from contracts.datalink import DataLinkExploreResult


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
