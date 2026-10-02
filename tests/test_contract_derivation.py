"""contract_derivation 的表驱动测试：60 runs 冤案 SQL 全部推导成功且降级永不拦截。

冤案依据见任务目录 research/execution-errors-inventory.md：
CAST 包裹聚合（r1/q17）、标量子查询 COUNT（r2/q02）、COUNT(DISTINCT)（r2/q08）、
先订单 SUM 再大区 AVG 的两层聚合（r1/q11、r3/q11）、带点别名（r2/q18）、
表达式占比（r2/q19）此前均被旧合同匹配器误拒。
"""

import pytest
from agent_runtime.contract_derivation import ContractDerivation, DerivedColumn, derive_contract
from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead


def _column(name: str) -> SchemaColumnRead:
    return SchemaColumnRead(name=name, type="TEXT", nullable=True)


def _table(name: str, *columns: str) -> SchemaTableRead:
    return SchemaTableRead(name=name, columns=[_column(column) for column in columns])


def _benchmark_schema() -> SchemaSummaryRead:
    """中文电商题集（datalink-fixed-r1..r3）用到的物理表结构。"""

    return SchemaSummaryRead(
        datasource_id="datasource_contract_derivation",
        dialect="mysql",
        tables=[
            _table(
                "customers",
                "customer_id",
                "nickname",
                "member_tier",
                "region",
                "city",
                "signup_date",
            ),
            _table(
                "orders",
                "order_id",
                "customer_id",
                "status",
                "order_ts",
                "payment_method",
                "shipping_fee",
            ),
            _table(
                "order_items",
                "order_item_id",
                "order_id",
                "product_id",
                "quantity",
                "line_amount",
            ),
            _table("products", "product_id", "category_id", "product_name"),
            _table("categories", "category_id", "category_name"),
            _table("reviews", "review_id", "order_id", "customer_id", "rating"),
            _table("shops", "shop_id", "shop_name", "city", "rating"),
            _table("events", "event_id", "customer_id", "event_type", "event_ts"),
        ],
    )


def _source(column: DerivedColumn) -> set[tuple[str, str]]:
    return {(item.table, item.column) for item in column.sources}


_CASES: list[tuple[str, str, str | None, dict[str, object]]] = [
    # (名称, SQL, dialect, 期望)
    (
        "两层聚合：先按订单 SUM 再按大区 AVG（r1/q11 误拒 SQL）",
        """
        SELECT c.region AS region, AVG(t.order_total) AS avg_order_amount
        FROM (
            SELECT o.order_id AS order_id,
                   o.customer_id AS customer_id,
                   SUM(oi.line_amount) AS order_total
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.order_id
            GROUP BY o.order_id, o.customer_id
        ) t
        JOIN customers c ON c.customer_id = t.customer_id
        GROUP BY c.region
        ORDER BY c.region
        """,
        None,
        {
            "tables": {"orders", "order_items", "customers"},
            "region": {("customers", "region")},
            "avg_order_amount": {("order_items", "line_amount")},
        },
    ),
    (
        "两层聚合：子查询直接内联（r3/q11 执行成功版本）",
        """
        SELECT c.region AS region, AVG(o.order_total) AS avg_order_amount
        FROM (
            SELECT oi.order_id AS order_id, SUM(oi.line_amount) AS order_total
            FROM order_items oi
            GROUP BY oi.order_id
        ) o
        JOIN orders od ON od.order_id = o.order_id
        JOIN customers c ON c.customer_id = od.customer_id
        GROUP BY c.region
        ORDER BY avg_order_amount DESC
        """,
        None,
        {
            "tables": {"orders", "order_items", "customers"},
            "region": {("customers", "region")},
            "avg_order_amount": {("order_items", "line_amount")},
        },
    ),
    (
        "简单两层表 AVG 聚合（r1/q11 执行成功版本）",
        """
        SELECT c.region AS region, AVG(oi.line_amount) AS avg_order_amount
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        JOIN customers c ON c.customer_id = o.customer_id
        GROUP BY c.region
        ORDER BY c.region
        """,
        None,
        {
            "tables": {"orders", "order_items", "customers"},
            "region": {("customers", "region")},
            "avg_order_amount": {("order_items", "line_amount")},
        },
    ),
    (
        "CAST 包裹的 AVG 聚合（r1/q17 冤案）",
        """
        SELECT c.member_tier AS member_tier, CAST(AVG(r.rating) AS DECIMAL(4,1)) AS avg_rating
        FROM customers c
        JOIN orders o ON c.customer_id = o.customer_id
        JOIN reviews r ON r.order_id = o.order_id
        GROUP BY c.member_tier
        """,
        None,
        {
            "tables": {"customers", "orders", "reviews"},
            "member_tier": {("customers", "member_tier")},
            "avg_rating": {("reviews", "rating")},
        },
    ),
    (
        "标量子查询里的 COUNT（r2/q02 冤案，限定字段）",
        """
        SELECT c.category_id AS category_id, c.category_name AS category_name,
               (SELECT COUNT(c2.category_id) FROM categories c2) AS category_count
        FROM categories c
        ORDER BY c.category_id
        """,
        None,
        {
            "tables": {"categories"},
            "category_id": {("categories", "category_id")},
            "category_name": {("categories", "category_name")},
            "category_count": {("categories", "category_id")},
        },
    ),
    (
        "标量子查询里的 COUNT（r2/q02 冤案，裸字段）",
        """
        SELECT c.category_id AS category_id, c.category_name AS category_name,
               (SELECT COUNT(category_id) FROM categories) AS category_count
        FROM categories c
        ORDER BY c.category_name ASC
        """,
        None,
        {
            "tables": {"categories"},
            "category_id": {("categories", "category_id")},
            "category_name": {("categories", "category_name")},
            "category_count": {("categories", "category_id")},
        },
    ),
    (
        "COUNT(DISTINCT ...) 与 LEFT JOIN 混合（r2/q08 冤案）",
        """
        SELECT o.status AS status, COUNT(DISTINCT o.order_id) AS order_cnt,
               COUNT(oi.order_id) AS item_rows, SUM(oi.line_amount) AS total_amount
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.order_id
        GROUP BY o.status
        ORDER BY o.status
        """,
        None,
        {
            "tables": {"orders", "order_items"},
            "status": {("orders", "status")},
            "order_cnt": {("orders", "order_id")},
            "item_rows": {("order_items", "order_id")},
            "total_amount": {("order_items", "line_amount")},
        },
    ),
    (
        "SELECT * 展开为物理表全部字段",
        "SELECT * FROM shops WHERE city = '上海'",
        None,
        {
            "tables": {"shops"},
            "shop_id": {("shops", "shop_id")},
            "shop_name": {("shops", "shop_name")},
            "city": {("shops", "city")},
            "rating": {("shops", "rating")},
        },
    ),
    (
        "SELECT s.* 同样展开",
        "SELECT s.* FROM shops s",
        None,
        {
            "tables": {"shops"},
            "shop_id": {("shops", "shop_id")},
            "shop_name": {("shops", "shop_name")},
            "city": {("shops", "city")},
            "rating": {("shops", "rating")},
        },
    ),
    (
        "CTE 传递投影（外层裸引用 CTE 输出）",
        """
        WITH regional_totals AS (
            SELECT c.region AS region, SUM(oi.line_amount) AS total_amount
            FROM customers c
            JOIN orders o ON o.customer_id = c.customer_id
            JOIN order_items oi ON oi.order_id = o.order_id
            GROUP BY c.region
        )
        SELECT region, total_amount FROM regional_totals ORDER BY total_amount DESC
        """,
        None,
        {
            "tables": {"customers", "orders", "order_items"},
            "region": {("customers", "region")},
            "total_amount": {("order_items", "line_amount")},
        },
    ),
    (
        "带点别名（r2/q18 语料原文，被旧报错教出来的畸形写法）",
        "SELECT shop_id AS `shops.shop_id`, shop_name AS `shops.shop_name`, "
        "city AS `shops.city`, rating AS `shops.rating` "
        "FROM shops WHERE city = '上海' "
        "GROUP BY shop_id, shop_name, city, rating ORDER BY shop_name",
        "mysql",
        {
            "tables": {"shops"},
            "shops.shop_id": {("shops", "shop_id")},
            "shops.shop_name": {("shops", "shop_name")},
            "shops.city": {("shops", "city")},
            "shops.rating": {("shops", "rating")},
        },
    ),
    (
        "表达式占比含标量子查询（r2/q19 冤案）",
        """
        SELECT c.category_name AS top_category_name, SUM(oi.quantity) AS top_category_quantity,
               (SELECT SUM(oi2.quantity) FROM order_items oi2) AS total_quantity_all,
               ROUND(SUM(oi.quantity) * 100.0 / (SELECT SUM(oi2.quantity) FROM order_items oi2), 4)
                   AS share_pct
        FROM order_items oi
        JOIN products p ON oi.product_id = p.product_id
        JOIN categories c ON p.category_id = c.category_id
        GROUP BY c.category_name
        ORDER BY top_category_quantity DESC
        LIMIT 1
        """,
        None,
        {
            "tables": {"order_items", "products", "categories"},
            "top_category_name": {("categories", "category_name")},
            "top_category_quantity": {("order_items", "quantity")},
            "total_quantity_all": {("order_items", "quantity")},
            "share_pct": {("order_items", "quantity")},
        },
    ),
    (
        "两个派生表 CROSS JOIN 的占比（r2/q19 执行成功版本）",
        """
        SELECT t.category_name AS top_category_name,
               t.total_quantity AS top_category_quantity,
               s.total_quantity_all AS total_quantity_all,
               ROUND(t.total_quantity * 100.0 / s.total_quantity_all, 4) AS share_pct
        FROM (
            SELECT c.category_name AS category_name, SUM(oi.quantity) AS total_quantity
            FROM order_items oi
            JOIN products p ON oi.product_id = p.product_id
            JOIN categories c ON p.category_id = c.category_id
            GROUP BY c.category_name
            ORDER BY total_quantity DESC
            LIMIT 1
        ) t
        CROSS JOIN (
            SELECT SUM(oi.quantity) AS total_quantity_all
            FROM order_items oi
            JOIN products p ON oi.product_id = p.product_id
            JOIN categories c ON p.category_id = c.category_id
        ) s
        """,
        None,
        {
            "tables": {"order_items", "products", "categories"},
            "top_category_name": {("categories", "category_name")},
            "top_category_quantity": {("order_items", "quantity")},
            "total_quantity_all": {("order_items", "quantity")},
            "share_pct": {("order_items", "quantity")},
        },
    ),
    (
        "NOT EXISTS 子查询中的表也计入物理表（r3/q20 冤案）",
        """
        SELECT COUNT(DISTINCT c.customer_id) AS browse_only_customers
        FROM customers c
        JOIN events e ON e.customer_id = c.customer_id
        WHERE e.event_type = 'view'
          AND e.event_ts BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'
          AND NOT EXISTS (
              SELECT 1 FROM orders o
              WHERE o.customer_id = c.customer_id
                AND o.order_ts BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'
          )
        """,
        None,
        {
            "tables": {"customers", "events", "orders"},
            "browse_only_customers": {("customers", "customer_id")},
        },
    ),
    (
        "无别名列投影名即结果列名（r2/q14 的 GROUP BY 1 写法）",
        """
        SELECT EXTRACT(YEAR_MONTH FROM o.order_ts) AS month, SUM(oi.line_amount) AS monthly_sales
        FROM orders o
        JOIN order_items oi ON o.order_id = oi.order_id
        WHERE o.status = 'completed'
        GROUP BY 1
        ORDER BY 1 ASC
        """,
        "mysql",
        {
            "tables": {"orders", "order_items"},
            "month": {("orders", "order_ts")},
            "monthly_sales": {("order_items", "line_amount")},
        },
    ),
]


@pytest.mark.parametrize(
    ("label", "sql", "dialect", "expected"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_derivation_resolves_real_corpus_sql(
    label: str,
    sql: str,
    dialect: str | None,
    expected: dict[str, object],
) -> None:
    derivation = derive_contract(sql, dialect=dialect, schema=_benchmark_schema())

    assert isinstance(derivation, ContractDerivation)
    assert derivation.derived_ok is True, (derivation.unsupported, derivation.projections)
    assert derivation.unsupported == ()
    assert derivation.schema_verified is True
    assert set(derivation.physical_tables) == expected["tables"]
    for projection in derivation.projections:
        expected_sources = expected.get(projection.name)
        if expected_sources is None:
            continue
        assert projection.status == "resolved", (projection.name, projection.status)
        assert _source(projection) == expected_sources, projection.name
    expected_names = {key for key in expected if key != "tables"}
    assert {item.name for item in derivation.projections} == expected_names


def test_derivation_resolves_without_schema_for_qualified_sql() -> None:
    """无 Schema 时限定字段的结构推导仍然成立。"""

    derivation = derive_contract(
        "SELECT c.region AS region, AVG(oi.line_amount) AS avg_order_amount "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN customers c ON c.customer_id = o.customer_id "
        "GROUP BY c.region ORDER BY c.region",
    )

    assert derivation.derived_ok is True
    assert derivation.schema_verified is False
    assert set(derivation.physical_tables) == {"orders", "order_items", "customers"}
    by_name = {item.name: item for item in derivation.projections}
    assert _source(by_name["region"]) == {("customers", "region")}
    assert _source(by_name["avg_order_amount"]) == {("order_items", "line_amount")}


def test_derivation_flags_unverified_fallback_when_qualification_fails() -> None:
    """Schema 在手但 qualification 失败时必须整体降级，不得假装已校验。"""

    derivation = derive_contract(
        "SELECT s.Region AS region FROM shops s",
        schema=_benchmark_schema(),
    )

    assert derivation.derived_ok is False
    assert derivation.schema_verified is False
    assert "schema_qualification_unavailable" in derivation.unsupported


def test_derivation_degrades_window_hidden_inside_cte() -> None:
    """藏在 CTE 内部的窗口函数同样降级，且不把窗口内部分列当成来源。"""

    derivation = derive_contract(
        "WITH ranked AS ("
        "SELECT shop_id, ROW_NUMBER() OVER (ORDER BY rating DESC) AS rank_no FROM shops"
        ") SELECT shop_id, rank_no FROM ranked",
        schema=_benchmark_schema(),
    )

    assert derivation.derived_ok is False
    assert "window_function" in derivation.unsupported
    by_name = {item.name: item for item in derivation.projections}
    assert by_name["shop_id"].status == "resolved"
    assert _source(by_name["shop_id"]) == {("shops", "shop_id")}
    assert by_name["rank_no"].status == "derived"
    assert by_name["rank_no"].sources == ()


def test_derivation_flags_duplicate_cte_output_alias_as_ambiguous() -> None:
    """无 Schema 时 CTE 重复输出别名不得静默取第一个。"""

    derivation = derive_contract(
        "WITH paired AS (SELECT city AS region, rating AS region FROM shops) "
        "SELECT region FROM paired",
    )

    assert derivation.derived_ok is False
    assert "ambiguous_projection" in derivation.unsupported
    assert derivation.projections[0].status == "derived"
    assert derivation.projections[0].sources == ()


_DEGRADATION_CASES: list[tuple[str, str, str, set[str]]] = [
    # (名称, SQL, 期望降级原因, 期望仍然推导出的物理表)
    (
        "窗口函数 OVER() 降级且不抛异常",
        "SELECT shop_id, shop_name, ROW_NUMBER() OVER (ORDER BY rating DESC) AS rank_no FROM shops",
        {"window_function"},
        {"shops"},
    ),
    (
        "UNION 分支降级且不抛异常",
        "SELECT shop_id FROM shops UNION SELECT customer_id FROM customers",
        {"set_operation"},
        {"shops", "customers"},
    ),
    (
        "解析失败降级",
        "SELEC shop_id FROM shops",
        {"parse_error"},
        set(),
    ),
    (
        "多语句降级",
        "SELECT shop_id FROM shops; SELECT city FROM shops",
        {"multiple_statements"},
        set(),
    ),
    (
        "非查询语句降级",
        "UPDATE shops SET rating = 5.0",
        {"non_query_statement"},
        set(),
    ),
    (
        "空语句降级",
        "   ",
        {"empty_statement"},
        set(),
    ),
]


@pytest.mark.parametrize(
    ("label", "sql", "reasons", "tables"),
    _DEGRADATION_CASES,
    ids=[case[0] for case in _DEGRADATION_CASES],
)
def test_derivation_degrades_without_raising(
    label: str, sql: str, reasons: set[str], tables: set[str]
) -> None:
    derivation = derive_contract(sql, schema=_benchmark_schema())

    assert isinstance(derivation, ContractDerivation)
    assert derivation.derived_ok is False
    assert reasons <= set(derivation.unsupported)
    assert set(derivation.physical_tables) == tables
    for projection in derivation.projections:
        assert projection.status in {"resolved", "derived"}


def test_derivation_flags_window_projection_as_derived_but_keeps_partial_facts() -> None:
    derivation = derive_contract(
        "SELECT shop_id, shop_name, ROW_NUMBER() OVER (ORDER BY rating DESC) AS rank_no FROM shops",
        schema=_benchmark_schema(),
    )

    by_name = {item.name: item for item in derivation.projections}
    assert by_name["shop_id"].status == "resolved"
    assert _source(by_name["shop_id"]) == {("shops", "shop_id")}
    assert by_name["rank_no"].status == "derived"
    assert "window_function" in derivation.unsupported


def test_derivation_returns_first_branch_names_for_union() -> None:
    derivation = derive_contract(
        "SELECT shop_id FROM shops UNION SELECT customer_id FROM customers",
        schema=_benchmark_schema(),
    )

    assert derivation.derived_ok is False
    assert "set_operation" in derivation.unsupported
    assert [item.name for item in derivation.projections] == ["shop_id"]
    assert all(item.status == "derived" for item in derivation.projections)


def test_derivation_degrades_star_without_schema() -> None:
    derivation = derive_contract("SELECT * FROM shops")

    assert derivation.derived_ok is False
    assert "star_unexpanded" in derivation.unsupported
    assert [item.name for item in derivation.projections] == ["*"]
    assert all(item.status == "derived" for item in derivation.projections)


def test_derivation_flags_ambiguous_bare_column_as_derived() -> None:
    """裸字段歧义不猜测归属，标记 derived 并降级。"""

    derivation = derive_contract(
        "SELECT customer_id FROM customers c JOIN orders o ON o.customer_id = c.customer_id",
        schema=_benchmark_schema(),
    )

    assert derivation.derived_ok is False
    assert "ambiguous_projection" in derivation.unsupported
    assert derivation.projections[0].status == "derived"


def test_derivation_leaves_engine_dependent_name_unnamed() -> None:
    """无别名的非列投影名依赖引擎，不猜名、标记未推导。"""

    derivation = derive_contract("SELECT COUNT(*) FROM shops", schema=_benchmark_schema())

    assert derivation.derived_ok is False
    assert "unnamed_projection" in derivation.unsupported
    assert derivation.projections[0].name == ""
    assert derivation.projections[0].status == "derived"


def test_derivation_unnamed_projection_reason_on_recursive_cte() -> None:
    """递归 CTE 自引用无法追溯，必须带降级原因而不是静默失败。"""

    derivation = derive_contract(
        "WITH RECURSIVE counter AS ("
        "SELECT 1 AS n UNION ALL SELECT n + 1 FROM counter WHERE n < 10) "
        "SELECT n FROM counter",
        schema=_benchmark_schema(),
    )

    assert derivation.derived_ok is False
    assert derivation.unsupported
    assert derivation.projections[0].status == "derived"


def test_derivation_resolves_unaliased_column_projection() -> None:
    derivation = derive_contract(
        "SELECT o.status, COUNT(*) AS cnt FROM orders o GROUP BY o.status",
        schema=_benchmark_schema(),
    )

    assert derivation.derived_ok is True
    by_name = {item.name: item for item in derivation.projections}
    assert by_name["status"].status == "resolved"
    assert _source(by_name["status"]) == {("orders", "status")}


def test_derivation_keeps_literal_projection_without_sources() -> None:
    derivation = derive_contract("SELECT 1 AS one", schema=_benchmark_schema())

    assert derivation.derived_ok is True
    assert len(derivation.projections) == 1
    assert derivation.projections[0].name == "one"
    assert derivation.projections[0].status == "resolved"
    assert derivation.projections[0].sources == ()
    assert derivation.physical_tables == ()
