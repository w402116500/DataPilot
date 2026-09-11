"""DataLink 结构抽取、脱敏字段画像和 Joinable 推断的行为测试。"""

from __future__ import annotations

from pathlib import Path

from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType
from contracts.status import DataSourceType

from server.connector import create_connector, resolve_source_path
from server.connector.base import SourceColumn
from server.extractor.tabular import TabularExtractor
from server.inferrer.correlated import CorrelationInferrer
from server.inferrer.distribution import DistributionInferrer
from server.inferrer.joinable import JoinableInferrer
from server.inferrer.synonym import SynonymInferrer
from server.models.graph import GraphEdge, GraphNode
from server.models.profile import ColumnProfile
from server.profiler.columns import ColumnProfiler

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_sqlite_extracts_tables_columns_contains_and_explicit_foreign_keys() -> None:
    connector = _demo_connector(DataSourceType.SQLITE, "demo/ecommerce.sqlite", "ds_demo")
    datasource = connector.inspect()

    structure = TabularExtractor().extract(datasource)

    assert sum(node.type == DataLinkNodeType.TABLE for node in structure.nodes) == 5
    assert sum(node.type == DataLinkNodeType.COLUMN for node in structure.nodes) == sum(
        len(table.columns) for table in datasource.tables
    )
    assert sum(edge.type == DataLinkEdgeType.CONTAINS for edge in structure.edges) == sum(
        len(table.columns) for table in datasource.tables
    )
    foreign_keys = [edge for edge in structure.edges if edge.type == DataLinkEdgeType.FOREIGN_KEY]
    assert len(foreign_keys) == 4
    assert all(edge.confidence == 1.0 for edge in foreign_keys)
    assert all(
        edge.evidence is not None and edge.evidence.kind == "explicit_foreign_key"
        for edge in foreign_keys
    )


def test_profiler_keeps_statistics_without_guessing_sensitive_values() -> None:
    connector = _demo_connector(DataSourceType.SQLITE, "demo/ecommerce.sqlite", "ds_demo")
    profiles = ColumnProfiler().profile_datasource(connector, connector.inspect())

    email_profile = next(profile for profile in profiles if profile.column_name == "email")
    amount_profile = next(profile for profile in profiles if profile.column_name == "total_amount")

    # 敏感列只能来自用户显式确认的遮蔽清单，不能根据列名或样例猜测。
    assert email_profile.is_sensitive is False
    assert email_profile.distinct_count > 0
    assert email_profile.top_values
    assert email_profile.sample_values
    assert email_profile.min_value is None
    assert email_profile.max_value is None
    assert amount_profile.is_sensitive is False
    assert amount_profile.dtype == "float"
    assert amount_profile.top_values
    assert amount_profile.min_value is not None
    assert amount_profile.max_value is not None


def test_joinable_uses_non_sensitive_cross_table_value_overlap() -> None:
    connector = _demo_connector(DataSourceType.SQLITE, "demo/ecommerce.sqlite", "ds_demo")
    datasource = connector.inspect()
    structure = TabularExtractor().extract(datasource)
    profiles = ColumnProfiler().profile_datasource(connector, datasource)

    edges = JoinableInferrer().infer(structure.nodes, profiles)

    assert edges
    assert all(edge.type == DataLinkEdgeType.JOINABLE for edge in edges)
    assert all(edge.confidence >= 0.1 for edge in edges)
    assert all(
        edge.evidence is not None and edge.evidence.kind == "value_overlap" for edge in edges
    )
    assert all("email" not in f"{edge.source_id}:{edge.target_id}" for edge in edges)


def test_csv_has_one_table_and_never_produces_cross_table_joinable_edges() -> None:
    connector = _demo_connector(DataSourceType.CSV, "demo/ecommerce_flat.csv", "ds_flat")
    datasource = connector.inspect()
    structure = TabularExtractor().extract(datasource)
    profiles = ColumnProfiler().profile_datasource(connector, datasource)

    assert [node.name for node in structure.nodes if node.type == DataLinkNodeType.TABLE] == [
        "dataset"
    ]
    assert JoinableInferrer().infer(structure.nodes, profiles) == ()


def test_joinable_skips_high_cardinality_boolean_and_incompatible_types() -> None:
    left = GraphNode(
        id="column:ds:left:identifier",
        type=DataLinkNodeType.COLUMN,
        name="identifier",
        table_name="left",
    )
    right = GraphNode(
        id="column:ds:right:identifier",
        type=DataLinkNodeType.COLUMN,
        name="identifier",
        table_name="right",
    )
    high_cardinality = _profile(left.id, "text", distinct_count=901, values=("same",))
    compatible = _profile(right.id, "text", values=("same",))
    assert JoinableInferrer().infer((left, right), (high_cardinality, compatible)) == ()

    boolean = _profile(left.id, "boolean", values=(True, False))
    assert JoinableInferrer().infer((left, right), (boolean, compatible)) == ()

    numeric = _profile(left.id, "integer", values=(1, 2))
    temporal = _profile(right.id, "date", values=("2024-01-01", "2024-01-02"))
    assert JoinableInferrer().infer((left, right), (numeric, temporal)) == ()


def test_joinable_threshold_accepts_numeric_and_text_identifier_values() -> None:
    left = GraphNode(
        id="column:ds:orders:customer_id",
        type=DataLinkNodeType.COLUMN,
        name="customer_id",
        table_name="orders",
    )
    right = GraphNode(
        id="column:ds:customers:id",
        type=DataLinkNodeType.COLUMN,
        name="id",
        table_name="customers",
    )
    left_profile = _profile(left.id, "integer", values=(1, 2, 3, 4, 5))
    right_profile = _profile(right.id, "text", values=("1", "x", "y", "z", "w"))

    edges = JoinableInferrer(overlap_threshold=0.1).infer(
        (left, right), (left_profile, right_profile)
    )

    assert len(edges) == 1
    assert edges[0].confidence == 0.2


def test_profiler_detects_zero_one_csv_values_as_boolean() -> None:
    profile = ColumnProfiler().profile_column(
        "ds_flat",
        "dataset",
        SourceColumn(name="is_paid", dtype="integer", nullable=False, is_primary_key=False),
        ("0", "1", "0", "1"),
    )

    assert profile.dtype == "boolean"


def test_profiler_does_not_detect_format_sensitive_values_without_explicit_policy() -> None:
    profile = ColumnProfiler().profile_column(
        "ds_flat",
        "dataset",
        SourceColumn(name="contact", dtype="text", nullable=True, is_primary_key=False),
        (
            "alice@example.com",
            "bob@example.com",
            "carol@example.com",
            "dan@example.com",
            "eve@example.com",
        ),
    )

    assert profile.is_sensitive is False
    assert profile.top_values
    assert profile.sample_values


def test_synonym_inferrer_uses_fixed_groups_and_skips_same_table() -> None:
    orders_id = GraphNode(
        id="column:ds:orders:customer_id",
        type=DataLinkNodeType.COLUMN,
        name="customer_id",
        table_name="orders",
    )
    customers_id = GraphNode(
        id="column:ds:customers:user_id",
        type=DataLinkNodeType.COLUMN,
        name="user_id",
        table_name="customers",
    )
    orders_status = GraphNode(
        id="column:ds:orders:status",
        type=DataLinkNodeType.COLUMN,
        name="status",
        table_name="orders",
    )
    customers_state = GraphNode(
        id="column:ds:customers:state",
        type=DataLinkNodeType.COLUMN,
        name="state",
        table_name="customers",
    )

    edges = SynonymInferrer().infer(
        (orders_id, customers_id, orders_status, customers_state),
        tuple(
            _profile(node.id, "text", values=("x",))
            for node in (
                orders_id,
                customers_id,
                orders_status,
                customers_state,
            )
        ),
    )

    assert {(edge.source_id, edge.target_id) for edge in edges} == {
        (orders_id.id, customers_id.id),
        (orders_status.id, customers_state.id),
    }
    assert all(edge.type == DataLinkEdgeType.SEMANTIC_SYNONYM for edge in edges)


def test_distribution_inferrer_uses_only_same_dtype_profiles() -> None:
    left = GraphNode(
        id="column:ds:left:amount",
        type=DataLinkNodeType.COLUMN,
        name="amount",
        table_name="left",
    )
    right = GraphNode(
        id="column:ds:right:total",
        type=DataLinkNodeType.COLUMN,
        name="total",
        table_name="right",
    )
    incompatible = GraphNode(
        id="column:ds:right:label",
        type=DataLinkNodeType.COLUMN,
        name="label",
        table_name="right",
    )
    edges = DistributionInferrer().infer(
        (left, right, incompatible),
        (
            _profile(left.id, "float", min_value=0, max_value=100),
            _profile(right.id, "float", min_value=20, max_value=80),
            _profile(incompatible.id, "text", values=("20", "80")),
        ),
    )

    assert len(edges) == 1
    assert edges[0].type == DataLinkEdgeType.DISTRIBUTION_SIMILAR
    assert edges[0].confidence == 0.6


def test_correlation_inferrer_aligns_only_joinable_rows_and_skips_foreign_key() -> None:
    order_key = GraphNode(
        id="column:ds:orders:customer_id",
        type=DataLinkNodeType.COLUMN,
        name="customer_id",
        table_name="orders",
    )
    customer_key = GraphNode(
        id="column:ds:customers:id",
        type=DataLinkNodeType.COLUMN,
        name="id",
        table_name="customers",
    )
    order_amount = GraphNode(
        id="column:ds:orders:amount",
        type=DataLinkNodeType.COLUMN,
        name="amount",
        table_name="orders",
    )
    customer_score = GraphNode(
        id="column:ds:customers:score",
        type=DataLinkNodeType.COLUMN,
        name="score",
        table_name="customers",
    )
    profiles = (
        _profile(order_key.id, "integer", values=(1, 2, 3, 4, 5, 6)),
        _profile(customer_key.id, "integer", values=(1, 2, 3, 4, 5, 6)),
        _profile(order_amount.id, "float", values=(10, 20, 30, 40, 50, 60)),
        _profile(customer_score.id, "float", values=(1, 2, 3, 4, 5, 6)),
    )
    joinable = GraphEdge(
        id="edge:joinable:orders:customer_id:customers:id",
        source_id=order_key.id,
        target_id=customer_key.id,
        type=DataLinkEdgeType.JOINABLE,
        confidence=1.0,
        evidence=DataLinkEdgeEvidenceRead(kind="value_overlap", summary="test"),
    )
    foreign_key = GraphEdge(
        id="edge:foreign_key:orders:customer_id:customers:id",
        source_id=order_key.id,
        target_id=customer_key.id,
        type=DataLinkEdgeType.FOREIGN_KEY,
        confidence=1.0,
    )
    connector = _RowsConnector(
        {
            "orders": [{"customer_id": index, "amount": index * 10} for index in range(1, 7)],
            "customers": [{"id": index, "score": index} for index in range(1, 7)],
        }
    )

    edges = CorrelationInferrer().infer(
        (order_key, customer_key, order_amount, customer_score),
        profiles,
        (joinable, foreign_key),
        connector,
    )

    assert len(edges) == 1
    assert edges[0].type == DataLinkEdgeType.CORRELATED
    assert edges[0].confidence == 1.0
    assert edges[0].properties["aligned_rows"] == 6


class _RowsConnector:
    def __init__(self, rows_by_table: dict[str, list[dict[str, object]]]) -> None:
        self.rows_by_table = rows_by_table

    def sample_rows(self, table_name: str, limit: int = 1_000) -> list[dict[str, object]]:
        return self.rows_by_table[table_name][:limit]


def _demo_connector(source_type: DataSourceType, source_ref: str, datasource_id: str):
    source_path = resolve_source_path(PROJECT_ROOT / "data", source_ref, source_type)
    return create_connector(source_type, source_path, datasource_id, 1)


def _profile(
    column_id: str,
    dtype: str,
    *,
    distinct_count: int | None = None,
    values: tuple[str | int | float | bool, ...] = (),
    min_value: str | int | float | None = None,
    max_value: str | int | float | None = None,
    sensitive: bool = False,
) -> ColumnProfile:
    return ColumnProfile(
        id=f"profile:{column_id}",
        column_id=column_id,
        column_name=column_id.rsplit(":", maxsplit=1)[-1],
        dtype=dtype,
        null_rate=0.0,
        distinct_count=distinct_count if distinct_count is not None else len(values),
        unique_rate=1.0,
        top_values=values,
        sample_values=(),
        min_value=min_value,
        max_value=max_value,
        is_sensitive=sensitive,
    )
