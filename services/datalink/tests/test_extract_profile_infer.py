"""DataLink 结构抽取、脱敏字段画像和 Joinable 推断的行为测试。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from contracts.datalink import (
    DataLinkEdgeEvidenceRead,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkNodeType,
)
from contracts.sensitive_fields import SensitiveFieldPolicy
from contracts.status import DataSourceType

from server.connector import ConnectorError, create_connector, resolve_source_path
from server.connector.base import (
    MAX_DISTINCT_VALUES,
    DatasourceInfo,
    SourceColumn,
    SourceTable,
)
from server.extractor.tabular import TabularExtractor
from server.inferrer.correlated import CorrelationInferrer
from server.inferrer.distribution import DistributionInferrer
from server.inferrer.joinable import JoinableInferrer
from server.inferrer.synonym import SynonymInferrer
from server.models.graph import GraphEdge, GraphNode, GraphStructure
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


class _DistinctRowsConnector:
    """带全表 distinct 桩的 Connector 替身，用于隔离验证典型取值确认分支。"""

    def __init__(
        self,
        rows: list[dict[str, object]],
        distinct: tuple[str, ...] = (),
    ) -> None:
        self.rows = rows
        self.distinct = distinct
        self.distinct_calls: list[tuple[str, str, int]] = []

    def inspect(self) -> None:
        raise AssertionError("profile_datasource must not re-inspect the datasource")

    def sample_rows(self, table_name: str, limit: int = 1_000) -> list[dict[str, object]]:
        return self.rows[:limit]

    def distinct_values(
        self, table_name: str, column_name: str, limit: int = MAX_DISTINCT_VALUES
    ) -> tuple[str, ...]:
        self.distinct_calls.append((table_name, column_name, limit))
        return self.distinct

    def close(self) -> None:
        pass


class _FailingDistinctConnector(_DistinctRowsConnector):
    """distinct_values 抛出 ConnectorError，模拟全表确认失败。"""

    def distinct_values(
        self, table_name: str, column_name: str, limit: int = MAX_DISTINCT_VALUES
    ) -> tuple[str, ...]:
        self.distinct_calls.append((table_name, column_name, limit))
        raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "full table scan failed")


def _single_column_datasource(column: SourceColumn) -> DatasourceInfo:
    return DatasourceInfo(
        datasource_id="ds_typical",
        source_type=DataSourceType.SQLITE,
        schema_revision=1,
        tables=[
            SourceTable(
                name="orders",
                columns=[column],
                row_count=None,
            )
        ],
    )


def test_profiler_confirms_full_table_typical_values_for_low_cardinality() -> None:
    rows = [{"status": value} for value in ("pending", "paid", "canceled")]
    connector = _DistinctRowsConnector(rows, ("paid", "pending", "canceled", "refunded"))
    column = SourceColumn(name="status", dtype="text", nullable=False, is_primary_key=False)

    profiles = ColumnProfiler().profile_datasource(connector, _single_column_datasource(column))

    assert profiles[0].typical_values == ("paid", "pending", "canceled", "refunded")
    assert connector.distinct_calls == [("orders", "status", MAX_DISTINCT_VALUES)]


def test_profiler_normalizes_non_string_full_table_values() -> None:
    """connector 返回非字符串标量时确认路径仍按字符串归并，不让 Build 崩溃。"""

    rows = [{"priority": value} for value in (1, 2, 3)]
    connector = _DistinctRowsConnector(rows, (2, 1, 3))
    column = SourceColumn(name="priority", dtype="integer", nullable=False, is_primary_key=False)

    profiles = ColumnProfiler().profile_datasource(connector, _single_column_datasource(column))

    assert profiles[0].typical_values == ("2", "1", "3")


def test_profiler_keeps_sample_typical_values_before_full_table_confirmation() -> None:
    column = SourceColumn(name="status", dtype="text", nullable=True, is_primary_key=False)

    profile = ColumnProfiler().profile_column(
        "ds_typical", "orders", column, ("已取消", "已取消", "已支付", None)
    )

    assert profile.typical_values == ("已取消", "已支付")


def test_profiler_skips_confirmation_for_high_cardinality_columns() -> None:
    rows = [{"id": index} for index in range(40)]
    connector = _DistinctRowsConnector(rows)
    column = SourceColumn(name="id", dtype="integer", nullable=False, is_primary_key=True)

    profiles = ColumnProfiler().profile_datasource(connector, _single_column_datasource(column))

    assert profiles[0].distinct_count == 40
    assert profiles[0].typical_values == ()
    assert connector.distinct_calls == []


def test_profiler_clears_typical_values_when_full_table_exceeds_limit() -> None:
    rows = [{"status": f"v{index:02d}"} for index in range(10)]
    connector = _DistinctRowsConnector(rows, tuple(f"v{index:02d}" for index in range(25)))
    column = SourceColumn(name="status", dtype="text", nullable=False, is_primary_key=False)

    profiles = ColumnProfiler().profile_datasource(connector, _single_column_datasource(column))

    assert profiles[0].distinct_count == 10
    assert profiles[0].typical_values == ()


def test_profiler_sensitive_columns_skip_full_table_confirmation() -> None:
    profiler = ColumnProfiler(
        SensitiveFieldPolicy(mask_fields=frozenset({"email"}), confirmed=True)
    )
    connector = _DistinctRowsConnector(
        [{"email": "a@example.test"}, {"email": "b@example.test"}],
        ("a@example.test", "b@example.test"),
    )
    column = SourceColumn(name="email", dtype="text", nullable=False, is_primary_key=False)

    profiles = profiler.profile_datasource(connector, _single_column_datasource(column))

    assert profiles[0].is_sensitive is True
    assert profiles[0].typical_values == ()
    assert profiles[0].top_values == ()
    assert connector.distinct_calls == []


def test_profiler_confirmation_failure_keeps_empty_values_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connector = _FailingDistinctConnector([{"status": "paid"}])
    column = SourceColumn(name="status", dtype="text", nullable=False, is_primary_key=False)

    with caplog.at_level(logging.WARNING, logger="server.profiler.columns"):
        profiles = ColumnProfiler().profile_datasource(connector, _single_column_datasource(column))

    assert profiles[0].typical_values == ()
    assert profiles[0].top_values == ("paid",)
    warning = next(record for record in caplog.records if record.levelno == logging.WARNING)
    assert getattr(warning, "datasource_id", None) == "ds_typical"
    assert getattr(warning, "column_name", None) == "status"
    assert getattr(warning, "error_code", None) == "BUILD_FAILED"


def test_profiler_truncates_long_typical_values() -> None:
    column = SourceColumn(name="label", dtype="text", nullable=True, is_primary_key=False)
    long_value = "标" * 300

    profile = ColumnProfiler().profile_column("ds_typical", "orders", column, (long_value, "ok"))

    # 同频次取值按稳定 token 次序排列，长值被截断到 200 字符。
    assert profile.typical_values == ("ok", "标" * 200)


def test_profiler_collects_full_table_typical_values_on_demo_sqlite() -> None:
    connector = _demo_connector(DataSourceType.SQLITE, "demo/ecommerce.sqlite", "ds_demo")

    profiles = ColumnProfiler().profile_datasource(connector, connector.inspect())

    status_profile = next(profile for profile in profiles if profile.column_name == "status")
    order_id_profile = next(profile for profile in profiles if profile.column_name == "order_id")
    assert set(status_profile.typical_values) == {
        "pending",
        "paid",
        "failed",
        "completed",
        "canceled",
        "refunded",
    }
    assert len(status_profile.typical_values) == status_profile.distinct_count
    assert order_id_profile.typical_values == ()


def test_profiler_infers_semantic_types_by_rule() -> None:
    profiler = ColumnProfiler()
    cases = [
        ("customer_id", "integer", (1, 2, 3), "identifier"),
        ("member_tier", "text", ("普通", "银卡", "普通", "金卡", "钻石"), "category"),
        ("region", "text", ("华东", "华北", "华东", "华南"), "category"),
        ("order_ts", "datetime", ("2025-01-01T08:00:00", "2025-01-02T09:00:00"), "timestamp"),
        ("created_date", "date", ("2025-01-01", "2025-01-02"), "date"),
        ("total_amount", "float", (199.0, 89.0), "monetary_value"),
        ("unit_price", "float", (199.0, 89.0), "monetary_value"),
        ("quantity", "integer", (2, 1, 3, 2, 1), "quantity"),
        ("is_paid", "integer", ("0", "1", "0", "1"), "boolean"),
    ]
    for name, dtype, values, expected in cases:
        profile = profiler.profile_column(
            "ds_rule",
            "t",
            SourceColumn(name=name, dtype=dtype, nullable=False, is_primary_key=False),
            values,
        )
        assert profile.semantic_type == expected, f"{name}: {profile.semantic_type}"


def test_profiler_keeps_unconfident_columns_without_semantic_type() -> None:
    profiler = ColumnProfiler()

    nickname = profiler.profile_column(
        "ds_rule",
        "t",
        SourceColumn(name="nickname", dtype="text", nullable=True, is_primary_key=False),
        ("张三", "李四", "王五", "赵六"),
    )
    assert nickname.semantic_type is None


def test_profiler_marks_primary_key_text_column_as_identifier() -> None:
    profiler = ColumnProfiler()

    profile = profiler.profile_column(
        "ds_rule",
        "orders",
        SourceColumn(name="order_no", dtype="text", nullable=False, is_primary_key=True),
        ("SO-2025-0001", "SO-2025-0002"),
    )
    assert profile.semantic_type == "identifier"


def test_builder_attaches_profile_semantic_type_to_column_nodes() -> None:
    from server.builder.service import _attach_semantic_types

    column_node = GraphNode(
        id="column:ds_rule:t:member_tier",
        type=DataLinkNodeType.COLUMN,
        name="member_tier",
        table_name="t",
    )
    table_node = GraphNode(
        id="table:ds_rule:t",
        type=DataLinkNodeType.TABLE,
        name="t",
    )
    structure = GraphStructure(nodes=(table_node, column_node), edges=(), pending_edges=())
    profile = ColumnProfile(
        id="profile:1",
        column_id=column_node.id,
        column_name="member_tier",
        dtype="text",
        null_rate=0.0,
        distinct_count=4,
        unique_rate=1.0,
        top_values=(),
        sample_values=(),
        semantic_type="category",
    )

    updated = _attach_semantic_types(structure, (profile,))
    assert updated.nodes[0].semantic_type is None
    assert updated.nodes[1].semantic_type == "category"
    assert _attach_semantic_types(structure, ()) is structure
