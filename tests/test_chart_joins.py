from agent_runtime.answer_materials import build_chart_spec
from agent_runtime.chart_joins import confirmed_join_labels
from agent_runtime.contracts import (
    AnalysisAssertion,
    AnalysisQueryAttempt,
    AnalysisVerifiedValue,
    analysis_fact_key,
)
from contracts.answer_materials import ChartIntent
from contracts.datalink import DataLinkSemanticContext, DataLinkSemanticRelationship
from contracts.datasources import (
    ForeignKeyRead,
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
    TableDataRead,
)

_SQL = (
    "SELECT customers.country AS region, SUM(orders.amount) AS spend "
    "FROM customers JOIN orders ON customers.customer_id = orders.customer_id "
    "GROUP BY customers.country"
)


def _column(name: str, column_type: str = "VARCHAR") -> SchemaColumnRead:
    return SchemaColumnRead(name=name, type=column_type, nullable=True)


def _schema(*, foreign_key: bool) -> SchemaSummaryRead:
    orders_keys = []
    if foreign_key:
        orders_keys.append(
            ForeignKeyRead(
                columns=["customer_id"],
                referenced_table="customers",
                referenced_columns=["customer_id"],
            )
        )
    return SchemaSummaryRead(
        datasource_id="datasource_chart",
        dialect="duckdb",
        tables=[
            SchemaTableRead(
                name="customers",
                columns=[_column("customer_id", "INTEGER"), _column("country")],
                primary_key=["customer_id"],
            ),
            SchemaTableRead(
                name="orders",
                columns=[
                    _column("order_id", "INTEGER"),
                    _column("customer_id", "INTEGER"),
                    _column("amount", "DOUBLE"),
                    _column("note"),
                ],
                foreign_keys=orders_keys,
            ),
        ],
    )


def _semantic(provenance: str, edge_type: str = "foreign_key") -> DataLinkSemanticContext:
    return DataLinkSemanticContext(
        mode="live",
        graph_version="graph_1",
        relationships=[
            DataLinkSemanticRelationship(
                provenance=provenance,
                source_table="orders",
                source_column="customer_id",
                target_table="customers",
                target_column="customer_id",
                edge_type=edge_type,
                confidence=0.4,
            )
        ],
    )


def test_confirmed_join_uses_schema_foreign_key() -> None:
    projection = confirmed_join_labels(_SQL, _schema(foreign_key=True), None, "duckdb")

    assert projection.labels == ("customers.customer_id = orders.customer_id",)
    assert projection.limitation is None


def test_inferred_or_joinable_relationship_is_not_confirmed() -> None:
    for provenance, edge_type in (
        ("inferred_candidate", "foreign_key"),
        ("semantic_mapping", "joinable"),
        ("unknown", "foreign_key"),
    ):
        projection = confirmed_join_labels(
            _SQL, _schema(foreign_key=False), _semantic(provenance, edge_type), "duckdb"
        )
        assert projection.labels == ()
        assert projection.limitation == "查询中的连接未对应到已确认关系。"


def test_confirmed_semantic_foreign_key_matches_without_schema_key() -> None:
    projection = confirmed_join_labels(
        _SQL, _schema(foreign_key=False), _semantic("database_foreign_key"), "duckdb"
    )

    assert projection.labels == ("customers.customer_id = orders.customer_id",)


def test_model_text_does_not_confirm_a_join() -> None:
    sql = (
        "SELECT customers.country AS region FROM customers "
        "JOIN orders ON customers.country = orders.note"
    )
    projection = confirmed_join_labels(sql, _schema(foreign_key=True), None, "duckdb")

    assert projection.labels == ()
    assert "customer_id" not in (projection.limitation or "")
    assert projection.limitation == "查询中的连接未对应到已确认关系。"


def test_unparsed_sql_leaves_confirmed_joins_empty() -> None:
    projection = confirmed_join_labels(
        "SELECT 1; SELECT 2", _schema(foreign_key=True), None, "duckdb"
    )

    assert projection.labels == ()
    assert projection.limitation == "实际查询的 Join 未能核对。"


def _series_attempt(audit_id: str, rows: list[list[object]], sql: str) -> AnalysisQueryAttempt:
    dimensions = {"region": "AU"}
    return AnalysisQueryAttempt(
        id="Q1" if audit_id == "audit_series" else "Q2",
        requirement_ids=["R1"],
        assertion_ids=["R1.A1"],
        assertions=[
            AnalysisAssertion(
                id="R1.A1",
                requirement_id="R1",
                description="地区消费",
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "消费",
                        "value_field": "spend",
                        "dimension_fields": ["region"],
                        "required": True,
                    }
                ],
            )
        ],
        sql=sql,
        status="evidenced",
        valid=True,
        audit_log_id=audit_id,
        safe_result=TableDataRead(columns=["region", "spend"], rows=rows, row_count=len(rows)),
        verified_values=[
            AnalysisVerifiedValue(
                name="消费",
                value=10,
                assertion_id="R1.A1",
                fact_key=analysis_fact_key("spend", dimensions),
                dimensions=dimensions,
            )
        ],
    )


def test_chart_coverage_compares_region_with_sibling_query() -> None:
    chart = _series_attempt("audit_series", [["AU", 10]] * 100, _SQL)
    regions = ["AU", "BR", "CA", "CN", "DE", "FR", "JP", "US"]
    sibling = _series_attempt(
        "audit_regions",
        [[region, index] for index, region in enumerate(regions)],
        "SELECT customers.country AS region, SUM(orders.amount) AS spend FROM customers "
        "GROUP BY customers.country",
    )
    spec = build_chart_spec(
        ChartIntent(
            relative_path="charts/regions.png",
            source_ref="audit_series",
            x_field="region",
            y_metric="spend",
        ),
        chart,
        schema=_schema(foreign_key=True),
        sibling_attempts=(sibling,),
        dialect="duckdb",
    )

    assert spec.confirmed_joins == ["customers.customer_id = orders.customer_id"]
    assert "region 1/8" in spec.coverage
    assert "未覆盖 BR、CA、CN、DE、FR、JP、US" in spec.coverage
    forbidden = ("规格声明", "未核验", "系统未检查")
    assert all(phrase not in " ".join(spec.limitations) for phrase in forbidden)
    assert "region 1/8" in " ".join(spec.limitations)
