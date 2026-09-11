from agent_runtime.contracts import (
    AnalysisAggregateConstraint,
    AnalysisAssertion,
    AnalysisColumnConstraint,
    AnalysisFilterConstraint,
    AnalysisGroupByConstraint,
    AnalysisSourceConstraint,
)
from agent_runtime.query_protocol import (
    authoritative_result_columns,
    preflight_discovery_scope,
    preflight_sql_contract,
)
from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead


def _orders_and_items_schema() -> SchemaSummaryRead:
    return SchemaSummaryRead(
        datasource_id="datasource_query_protocol",
        dialect="duckdb",
        tables=[
            SchemaTableRead(
                name="orders",
                columns=[
                    SchemaColumnRead(name="order_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="customer_id", type="INTEGER", nullable=True),
                    SchemaColumnRead(name="amount", type="DOUBLE", nullable=False),
                ],
            ),
            SchemaTableRead(
                name="order_items",
                columns=[
                    SchemaColumnRead(name="order_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="amount", type="DOUBLE", nullable=False),
                ],
            ),
        ],
    )


def _customers_schema() -> SchemaSummaryRead:
    return SchemaSummaryRead(
        datasource_id="datasource_customers",
        dialect="sqlite",
        tables=[
            SchemaTableRead(
                name="customers",
                columns=[
                    SchemaColumnRead(name="customer_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="signup_date", type="DATE", nullable=False),
                ],
            )
        ],
    )


def _monthly_customers_assertion() -> AnalysisAssertion:
    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="按月统计注册客户数",
        source_tables=["customers"],
        dimensions=["signup_month"],
        result_columns=["signup_month", "customer_count"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="customers"),
            AnalysisAggregateConstraint(
                kind="aggregate",
                function="count",
                column="customer_id",
                alias="customer_count",
            ),
            AnalysisGroupByConstraint(kind="group_by", columns=["signup_date"]),
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "月度注册客户数",
                "value_field": "customer_count",
                "dimension_fields": ["signup_month"],
            }
        ],
    )


def _qualified_count_assertion(column: str = "orders.order_id") -> AnalysisAssertion:
    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["orders", "order_items"],
        result_columns=["order_count"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisSourceConstraint(kind="source", table="order_items"),
            AnalysisAggregateConstraint(
                kind="aggregate", function="count", column=column, alias="order_count"
            ),
        ],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "订单总数",
                "field": "order_count",
                "required": True,
            }
        ],
    )


def _orders_assertion(*, alias: str = "total_orders") -> AnalysisAssertion:
    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["orders"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisAggregateConstraint(
                kind="aggregate", function="count", column="*", alias=alias
            ),
            AnalysisGroupByConstraint(kind="group_by", columns=["purpose"]),
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "订单总数",
                "value_field": alias,
                "dimension_fields": ["purpose"],
            }
        ],
    )


def test_alias_mismatch_is_rejected_before_execution() -> None:
    result = preflight_sql_contract(
        "SELECT purpose, COUNT(*) AS order_count FROM orders GROUP BY purpose",
        dialect="duckdb",
        assertions=[_orders_assertion()],
    )

    assert result.valid is False
    assert any("AGGREGATE_MISMATCH" in finding.code for finding in result.findings)
    assert any("RESULT_COLUMN_MISSING" in finding.code for finding in result.findings)
    assert result.expected_columns == ["total_orders", "purpose"]


def test_null_filter_contract_matches_is_null_and_is_not_null() -> None:
    def assertion(operator: str) -> AnalysisAssertion:
        return AnalysisAssertion(
            id="R1.A1",
            requirement_id="R1",
            description="检查客户编号空值",
            source_tables=["customers"],
            result_columns=["customer_id"],
            sql_constraints=[
                AnalysisFilterConstraint(
                    kind="filter", column="customers.customer_id", operator=operator, value=None
                )
            ],
            claim_extractions=[
                {"mode": "scalar", "name": "客户编号", "field": "customer_id", "required": True}
            ],
        )

    is_null = preflight_sql_contract(
        "SELECT customer_id FROM customers WHERE customer_id IS NULL",
        dialect="duckdb",
        assertions=[assertion("is_null")],
        schema=_customers_schema(),
    )
    is_not_null = preflight_sql_contract(
        "SELECT customer_id FROM customers WHERE customer_id IS NOT NULL",
        dialect="duckdb",
        assertions=[assertion("is_not_null")],
        schema=_customers_schema(),
    )

    assert is_null.valid is True
    assert is_not_null.valid is True


def test_null_filter_contract_resolves_table_alias_across_join() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="检查订单缺失客户编号",
        source_tables=["orders", "order_items"],
        result_columns=["order_id"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisSourceConstraint(kind="source", table="order_items"),
            AnalysisFilterConstraint(
                kind="filter",
                column="orders.customer_id",
                operator="is_null",
                value=None,
            ),
        ],
        claim_extractions=[
            {"mode": "scalar", "name": "订单编号", "field": "order_id", "required": True}
        ],
    )

    result = preflight_sql_contract(
        "SELECT o.order_id FROM orders AS o "
        "JOIN order_items AS i ON o.order_id = i.order_id "
        "WHERE o.customer_id IS NULL",
        dialect="duckdb",
        assertions=[assertion],
        schema=_orders_and_items_schema(),
    )

    assert result.valid is True


def test_matching_aggregate_alias_and_group_passes() -> None:
    result = preflight_sql_contract(
        "SELECT purpose, COUNT(*) AS total_orders FROM orders GROUP BY purpose",
        dialect="duckdb",
        assertions=[_orders_assertion()],
    )

    assert result.valid is True
    assert result.findings == []


def test_time_bucket_group_by_matches_its_physical_source_column() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="按月统计注册客户数",
        source_tables=["customers"],
        dimensions=["signup_month"],
        result_columns=["signup_month", "customer_count"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="customers"),
            AnalysisAggregateConstraint(
                kind="aggregate",
                function="count",
                column="customer_id",
                alias="customer_count",
            ),
            AnalysisGroupByConstraint(kind="group_by", columns=["signup_date"]),
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "月度注册客户数",
                "value_field": "customer_count",
                "dimension_fields": ["signup_month"],
            }
        ],
    )

    result = preflight_sql_contract(
        "SELECT strftime('%Y-%m', signup_date) AS signup_month, "
        "COUNT(customer_id) AS customer_count FROM customers "
        "GROUP BY strftime('%Y-%m', signup_date)",
        dialect="sqlite",
        assertions=[assertion],
    )

    assert result.valid is True
    assert result.findings == []
    assert result.expected_columns == ["signup_month", "customer_count"]


def test_time_bucket_group_by_alias_matches_its_physical_source_column() -> None:
    result = preflight_sql_contract(
        "SELECT strftime('%Y-%m', signup_date) AS signup_month, "
        "COUNT(customer_id) AS customer_count FROM customers GROUP BY signup_month",
        dialect="sqlite",
        assertions=[_monthly_customers_assertion()],
        schema=_customers_schema(),
    )

    assert result.valid is True
    assert result.findings == []


def test_unrelated_group_by_alias_does_not_match_physical_source_column() -> None:
    schema = _orders_and_items_schema()
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="按订单分组",
        source_tables=["orders"],
        result_columns=["amount_bucket", "order_count"],
        sql_constraints=[
            AnalysisGroupByConstraint(kind="group_by", columns=["orders.order_id"]),
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "订单数",
                "value_field": "order_count",
                "dimension_fields": ["amount_bucket"],
            }
        ],
    )

    result = preflight_sql_contract(
        "SELECT ROUND(orders.amount) AS amount_bucket, COUNT(*) AS order_count "
        "FROM orders GROUP BY amount_bucket",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any("GROUP_BY_MISSING" in finding.code for finding in result.findings)


def test_group_by_expression_does_not_satisfy_an_unrelated_source_column() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="按月统计注册客户数",
        source_tables=["customers"],
        dimensions=["signup_month"],
        result_columns=["signup_month", "customer_count"],
        sql_constraints=[
            AnalysisGroupByConstraint(kind="group_by", columns=["country"]),
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "月度注册客户数",
                "value_field": "customer_count",
                "dimension_fields": ["signup_month"],
            }
        ],
    )

    result = preflight_sql_contract(
        "SELECT strftime('%Y-%m', signup_date) AS signup_month, "
        "COUNT(customer_id) AS customer_count FROM customers "
        "GROUP BY strftime('%Y-%m', signup_date)",
        dialect="sqlite",
        assertions=[assertion],
    )

    assert result.valid is False
    assert any(
        finding.code == "SQL_CONTRACT_GROUP_BY_MISSING:country" for finding in result.findings
    )


def test_cte_aggregate_is_checked_when_outer_select_projects_the_result() -> None:
    result = preflight_sql_contract(
        "WITH totals AS ("
        "SELECT COUNT(customer_id) AS total_customers FROM customers"
        ") SELECT total_customers FROM totals",
        dialect="sqlite",
        assertions=[
            AnalysisAssertion(
                id="R1.A1",
                requirement_id="R1",
                description="统计客户数",
                source_tables=["customers"],
                sql_constraints=[
                    AnalysisSourceConstraint(kind="source", table="customers"),
                    AnalysisAggregateConstraint(
                        kind="aggregate", function="count", column="customer_id"
                    ),
                ],
                claim_extractions=[
                    {
                        "mode": "scalar",
                        "name": "客户数",
                        "field": "total_customers",
                        "required": True,
                    }
                ],
            )
        ],
    )

    assert result.valid is True


def test_discovery_scope_accepts_cte_output_and_order_alias() -> None:
    result = preflight_discovery_scope(
        "WITH monthly AS ("
        "SELECT signup_date, COUNT(customer_id) AS customer_count "
        "FROM customers GROUP BY signup_date"
        ") SELECT signup_date, customer_count FROM monthly ORDER BY customer_count DESC",
        dialect="sqlite",
        schema=_customers_schema(),
        allowed_tables=["customers"],
        allowed_columns=["customers.signup_date", "customers.customer_id"],
    )

    assert result.valid is True
    assert result.findings == []


def test_discovery_scope_accepts_union_cte_output_aliases() -> None:
    """UNION/CTE 的结果列是派生字段，不应按物理裸字段重复计为歧义。"""

    result = preflight_discovery_scope(
        "WITH counts AS ("
        "SELECT 'customers' AS section, 'rows' AS metric, COUNT(*) AS value FROM customers "
        "UNION ALL SELECT 'customers', 'distinct_id', COUNT(DISTINCT customer_id) FROM customers"
        ") SELECT section, metric, value FROM counts ORDER BY metric",
        dialect="sqlite",
        schema=_customers_schema(),
        allowed_tables=["customers"],
        allowed_columns=["customers.customer_id"],
    )

    assert result.valid is True
    assert result.findings == []


def test_discovery_scope_accepts_nested_cte_projection_aliases() -> None:
    """多层 CTE 的 SELECT/GROUP/ORDER 别名必须沿 lineage 回溯到物理字段。"""

    result = preflight_discovery_scope(
        "WITH monthly AS ("
        "SELECT signup_date, COUNT(customer_id) AS customer_count "
        "FROM customers GROUP BY signup_date"
        "), trends AS ("
        "SELECT signup_date AS month, customer_count AS value FROM monthly"
        ") SELECT month, value FROM trends ORDER BY month",
        dialect="sqlite",
        schema=_customers_schema(),
        allowed_tables=["customers"],
        allowed_columns=["customers.signup_date", "customers.customer_id"],
    )

    assert result.valid is True
    assert result.findings == []


def test_discovery_scope_accepts_union_branches_with_shared_cte_output_names() -> None:
    """Sibling CTEs visible to a UNION branch must not make its outputs ambiguous."""

    result = preflight_discovery_scope(
        "WITH base AS ("
        "SELECT 'customers' AS metric, COUNT(*) AS n FROM customers "
        "UNION ALL SELECT 'events' AS metric, COUNT(*) AS n FROM customers"
        "), dates AS ("
        "SELECT 'signup' AS metric, COUNT(*) AS n FROM customers "
        "UNION ALL SELECT 'event' AS metric, COUNT(*) AS n FROM customers"
        ") SELECT metric, n FROM base UNION ALL SELECT metric, n FROM dates ORDER BY metric",
        dialect="sqlite",
        schema=_customers_schema(),
        allowed_tables=["customers"],
        allowed_columns=["customers.customer_id"],
    )

    assert result.valid is True
    assert result.findings == []


def test_discovery_scope_rejects_unknown_physical_column_inside_cte() -> None:
    result = preflight_discovery_scope(
        "WITH monthly AS ("
        "SELECT missing_value, COUNT(customer_id) AS customer_count "
        "FROM customers GROUP BY missing_value"
        ") SELECT missing_value, customer_count FROM monthly",
        dialect="sqlite",
        schema=_customers_schema(),
        allowed_tables=["customers"],
        allowed_columns=["customers.signup_date", "customers.customer_id"],
    )

    assert result.valid is False
    assert any(finding.code == "DISCOVERY_SCOPE_COLUMN_INVALID" for finding in result.findings)


def test_discovery_scope_rejects_ambiguous_bare_column_across_tables() -> None:
    result = preflight_discovery_scope(
        "SELECT order_id FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        schema=_orders_and_items_schema(),
        allowed_tables=["orders", "order_items"],
        allowed_columns=["orders.order_id", "order_items.order_id"],
    )

    assert result.valid is False
    assert any(finding.code == "DISCOVERY_SCOPE_COLUMN_AMBIGUOUS" for finding in result.findings)


def test_discovery_scope_rejects_unknown_outer_cte_output() -> None:
    result = preflight_discovery_scope(
        "WITH selected AS (SELECT amount AS total FROM orders) SELECT missing FROM selected",
        dialect="duckdb",
        schema=_orders_and_items_schema(),
        allowed_tables=["orders"],
        allowed_columns=["orders.amount"],
    )

    assert result.valid is False
    assert any(finding.code == "DISCOVERY_SCOPE_COLUMN_INVALID" for finding in result.findings)


def test_discovery_scope_rejects_partial_allowlist_for_nested_select_star() -> None:
    result = preflight_discovery_scope(
        "SELECT * FROM (SELECT * FROM orders) AS selected",
        dialect="duckdb",
        schema=_orders_and_items_schema(),
        allowed_tables=["orders"],
        allowed_columns=["orders.amount"],
    )

    assert result.valid is False
    assert any(finding.code == "DISCOVERY_SCOPE_COLUMN_FORBIDDEN" for finding in result.findings)


def test_discovery_scope_rejects_unknown_table_alias() -> None:
    result = preflight_discovery_scope(
        "SELECT wrong.amount FROM orders AS actual",
        dialect="duckdb",
        schema=_orders_and_items_schema(),
        allowed_tables=["orders"],
        allowed_columns=["orders.amount"],
    )

    assert result.valid is False
    assert any(finding.code == "DISCOVERY_SCOPE_COLUMN_INVALID" for finding in result.findings)


def test_aggregate_keeps_table_qualification_across_join() -> None:
    schema = _orders_and_items_schema()
    assertion = _qualified_count_assertion()

    wrong_source = preflight_sql_contract(
        "SELECT COUNT(order_items.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )
    correct_source = preflight_sql_contract(
        "SELECT COUNT(orders.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert wrong_source.valid is False
    assert any("AGGREGATE_MISMATCH" in finding.code for finding in wrong_source.findings)
    assert correct_source.valid is True


def test_aggregate_aliases_resolve_to_their_real_table() -> None:
    schema = _orders_and_items_schema()
    assertion = _qualified_count_assertion()

    result = preflight_sql_contract(
        "SELECT COUNT(oi.order_id) AS order_count "
        "FROM orders AS o JOIN order_items AS oi ON o.order_id = oi.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any("AGGREGATE_MISMATCH" in finding.code for finding in result.findings)


def test_count_distinct_is_not_a_direct_aggregate_contract_match() -> None:
    schema = _orders_and_items_schema()
    assertion = _qualified_count_assertion(column="orders.order_id")

    result = preflight_sql_contract(
        "SELECT COUNT(DISTINCT orders.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any("AGGREGATE_MISMATCH" in finding.code for finding in result.findings)


def test_case_and_window_aggregates_are_not_direct_contract_matches() -> None:
    schema = _orders_and_items_schema()
    assertion = _qualified_count_assertion(column="orders.amount")

    case_result = preflight_sql_contract(
        "SELECT CASE WHEN COUNT(orders.amount) > 0 THEN COUNT(orders.amount) ELSE 0 END "
        "AS order_count FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )
    window_result = preflight_sql_contract(
        "SELECT SUM(orders.amount) OVER (PARTITION BY orders.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert case_result.valid is False
    assert window_result.valid is False
    assert any("AGGREGATE_MISMATCH" in finding.code for finding in case_result.findings)
    assert any("AGGREGATE_MISMATCH" in finding.code for finding in window_result.findings)


def test_qualified_group_by_does_not_match_same_leaf_from_other_table() -> None:
    schema = _orders_and_items_schema()
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="按订单分组",
        source_tables=["orders", "order_items"],
        result_columns=["order_id", "amount_total"],
        sql_constraints=[
            AnalysisGroupByConstraint(kind="group_by", columns=["orders.order_id"]),
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "订单金额",
                "value_field": "amount_total",
                "dimension_fields": ["order_id"],
            }
        ],
    )

    result = preflight_sql_contract(
        "SELECT order_items.order_id AS order_id, SUM(orders.amount) AS amount_total "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id "
        "GROUP BY order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any("GROUP_BY_MISSING" in finding.code for finding in result.findings)


def test_qualified_result_projection_keeps_physical_table_scope() -> None:
    schema = _orders_and_items_schema()
    assertion = _qualified_count_assertion(column="orders.order_id")
    assertion.result_columns = ["orders.order_id", "order_count"]
    assertion.claim_extractions[0].field = "order_count"

    result = preflight_sql_contract(
        "SELECT order_items.order_id AS order_id, COUNT(orders.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any(
        "RESULT_COLUMN_MISSING:orders.order_id" in finding.code for finding in result.findings
    )


def test_cte_projection_preserves_physical_lineage_for_result_contract() -> None:
    schema = _orders_and_items_schema()
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="读取订单金额",
        source_tables=["orders"],
        result_columns=["orders.amount"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisColumnConstraint(kind="column", column="orders.amount"),
        ],
        claim_extractions=[
            {"mode": "scalar", "name": "金额", "field": "orders.amount", "required": True}
        ],
    )

    result = preflight_sql_contract(
        "WITH selected AS (SELECT amount FROM orders) SELECT amount FROM selected",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is True
    assert result.findings == []


def test_outer_cte_wrong_physical_source_is_rejected() -> None:
    schema = _orders_and_items_schema()
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="读取订单金额",
        source_tables=["orders"],
        result_columns=["orders.amount"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisColumnConstraint(kind="column", column="orders.amount"),
        ],
        claim_extractions=[
            {"mode": "scalar", "name": "金额", "field": "orders.amount", "required": True}
        ],
    )

    result = preflight_sql_contract(
        "WITH selected AS (SELECT amount FROM order_items) SELECT amount FROM selected",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any("COLUMN_MISSING:orders.amount" in finding.code for finding in result.findings)


def test_filter_inside_nested_scope_is_checked_against_physical_column() -> None:
    schema = _orders_and_items_schema()
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="筛选订单金额",
        source_tables=["orders"],
        result_columns=["amount"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisFilterConstraint(
                kind="filter", column="orders.amount", operator="gt", value=10
            ),
        ],
        claim_extractions=[{"mode": "scalar", "name": "金额", "field": "amount", "required": True}],
    )

    result = preflight_sql_contract(
        "WITH selected AS (SELECT amount FROM orders WHERE amount > 10) "
        "SELECT amount FROM selected",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is True
    assert result.findings == []


def test_unknown_outer_cte_output_fails_before_contract_comparison() -> None:
    schema = _orders_and_items_schema()
    result = preflight_sql_contract(
        "WITH selected AS (SELECT amount FROM orders) SELECT missing FROM selected",
        dialect="duckdb",
        assertions=[],
        schema=schema,
    )

    assert result.valid is False
    assert [finding.code for finding in result.findings] == ["SQL_CONTRACT_SCOPE_INVALID"]


def test_source_constraint_outside_assertion_scope_is_rejected() -> None:
    schema = _orders_and_items_schema()
    assertion = _qualified_count_assertion()
    assertion.source_tables = ["orders"]

    result = preflight_sql_contract(
        "SELECT COUNT(orders.order_id) AS order_count FROM orders",
        dialect="duckdb",
        assertions=[assertion],
        schema=schema,
    )

    assert result.valid is False
    assert any("SOURCE_OUT_OF_SCOPE" in finding.code for finding in result.findings)


def test_group_by_aggregate_is_rejected_before_gateway() -> None:
    result = preflight_sql_contract(
        "SELECT CASE WHEN COUNT(event_id) = 0 THEN 'none' ELSE 'some' END AS segment, "
        "COUNT(event_id) AS event_count FROM events "
        "GROUP BY CASE WHEN COUNT(event_id) = 0 THEN 'none' ELSE 'some' END",
        dialect="sqlite",
        assertions=[],
    )

    assert result.valid is False
    assert any(finding.code == "SQL_CONTRACT_GROUP_BY_AGGREGATE" for finding in result.findings)


def test_preflight_reports_missing_group_by_without_gateway_access() -> None:
    result = preflight_sql_contract(
        "SELECT purpose, COUNT(*) AS total_orders FROM orders",
        dialect="duckdb",
        assertions=[_orders_assertion()],
    )

    assert result.valid is False
    assert any(
        "SQL_CONTRACT_GROUP_BY_MISSING:purpose" == finding.code for finding in result.findings
    )


def test_assertion_without_ast_constraints_only_requires_declared_result_column() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="返回一个受控结果列",
        claim_extractions=[{"mode": "scalar", "name": "value", "field": "value", "required": True}],
    )

    result = preflight_sql_contract(
        "SELECT 1 AS value",
        dialect="sqlite",
        assertions=[assertion],
    )

    assert result.valid is True


def test_gateway_still_owns_sql_parse_errors() -> None:
    result = preflight_sql_contract(
        "SELECT COUNT(*) AS order_count FROM",
        dialect="duckdb",
        assertions=[_orders_assertion()],
    )

    assert result.valid is True
    assert result.expected_columns == ["total_orders", "purpose"]


def test_authoritative_result_columns_cover_all_contract_consumers() -> None:
    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["orders"],
        dimensions=["purpose"],
        sql_constraints=[
            AnalysisSourceConstraint(kind="source", table="orders"),
            AnalysisAggregateConstraint(
                kind="aggregate", function="count", column="*", alias="total_orders"
            ),
            AnalysisGroupByConstraint(kind="group_by", columns=["purpose"]),
        ],
        result_checks=[
            {"kind": "not_null", "required": True, "fields": ["purpose", "total_orders"]},
            {
                "kind": "equals",
                "required": False,
                "left": {"field": "total_orders", "selector": {"purpose": "credit_card"}},
                "right": {"field": "target_total", "selector": {"cohort": "current"}},
            },
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "订单总数",
                "value_field": "total_orders",
                "dimension_fields": ["purpose", "segment"],
            }
        ],
    )

    assert authoritative_result_columns([assertion]) == [
        "total_orders",
        "purpose",
        "segment",
        "target_total",
        "cohort",
    ]
