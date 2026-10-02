# 09-28-opening-protocol-derivation 大切换（prd R4、design §5）测试删留说明：
# preflight_sql_contract 不再把 SQL 与合同互相比较，以下 SQL_CONTRACT_* 匹配行为
# 及其测试被有意移除：
#   - AGGREGATE_MISMATCH / GROUP_BY_MISSING / FILTER_MISSING / TIME_*_MISSING /
#     COLUMN_MISSING / SOURCE_MISSING / SOURCE_OUT_OF_SCOPE / RESULT_COLUMN_MISSING
#     （q11/q17/q02/q08/q18 冤案的判定来源）
#   - SQL_CONTRACT_SCOPE_INVALID（字段无法在 Schema 唯一解析的提前判死；未知物理字段
#     的 retryable UNKNOWN_COLUMN 提示仍归 Data Gateway SQL Guard 所有）
#   - SQL_CONTRACT_GROUP_BY_AGGREGATE（SQL 合法性归 Gateway 执行错误处理）
# 保留的安全断言：Discovery 物理 allowlist（错误 JOIN、越权表/列、歧义字段）、
# 声明 source_tables 的物理解析（SQL_CONTRACT_PHYSICAL_REFERENCE_INVALID）、
# 解析错误不进入业务预检、authoritative_result_columns 的声明回退语义。
# SQL 与合同的结构对照改由 contract_derivation.derive_contract 推导（永不拦截），
# 其行为由 tests/test_contract_derivation.py 与 tests/test_derivation_spec.py 保护。
from agent_runtime.contracts import AnalysisAssertion
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


def _orders_assertion() -> AnalysisAssertion:
    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["orders"],
        claim_extractions=[
            {
                "mode": "series",
                "name": "订单总数",
                "value_field": "total_orders",
                "dimension_fields": ["purpose"],
            }
        ],
    )


def test_narrowed_preflight_derives_declared_result_columns() -> None:
    """expected_columns 回退来自声明：结果别名 + claim 字段，物理限定被剥离。"""

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="各大区平均订单金额",
        source_tables=["orders", "customers"],
        result_columns=["region", "avg_order_amount"],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "平均订单金额",
                "field": "avg_order_amount",
                "required": True,
            }
        ],
    )

    result = preflight_sql_contract(
        "SELECT region, AVG(amount) AS avg_order_amount FROM orders",
        dialect="duckdb",
        assertions=[assertion],
    )

    assert result.valid is True
    assert result.findings == []
    assert result.expected_columns == ["region", "avg_order_amount"]


def test_preflight_still_rejects_unknown_declared_source_table() -> None:
    """安全边界保留：声明的 source_tables 必须是当前冻结 Schema 中的真实表。"""

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["orders", "not_a_table"],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "订单总数",
                "field": "total_orders",
                "required": True,
            }
        ],
    )

    result = preflight_sql_contract(
        "SELECT COUNT(*) AS total_orders FROM orders",
        dialect="duckdb",
        assertions=[assertion],
        schema=_orders_and_items_schema(),
    )

    assert result.valid is False
    assert any(
        finding.code == "SQL_CONTRACT_PHYSICAL_REFERENCE_INVALID" for finding in result.findings
    )


def test_preflight_does_not_block_unresolvable_or_matched_sql() -> None:
    """推导/核对不拦截：无法用声明判断的 SQL 一律放行，交给 Guard 与结果核对。"""

    assertion = _orders_assertion()

    aggregate = preflight_sql_contract(
        "SELECT COUNT(DISTINCT orders.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[assertion],
        schema=_orders_and_items_schema(),
    )
    detail = preflight_sql_contract(
        "WITH selected AS (SELECT amount FROM orders) SELECT missing FROM selected",
        dialect="duckdb",
        assertions=[assertion],
        schema=_orders_and_items_schema(),
    )

    assert aggregate.valid is True
    assert detail.valid is True


def test_gateway_still_owns_sql_parse_errors() -> None:
    result = preflight_sql_contract(
        "SELECT COUNT(*) AS order_count FROM",
        dialect="duckdb",
        assertions=[_orders_assertion()],
    )

    assert result.valid is True
    assert result.expected_columns == ["total_orders", "purpose"]


def test_assertion_without_declared_result_columns_still_yields_claim_fields() -> None:
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
    assert result.expected_columns == ["value"]


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


def test_authoritative_result_columns_use_declared_aliases_not_physical_names() -> None:
    """声明回退只含结果别名与 claim 字段；物理限定取叶子名，结果检查不再参与。"""

    assertion = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="统计订单量",
        source_tables=["orders"],
        dimensions=["orders.purpose"],
        result_columns=["total_orders"],
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
    ]
