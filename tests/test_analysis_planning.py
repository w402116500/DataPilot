import pytest
from agent_runtime.analysis_planning import materialize_analysis_plan
from agent_runtime.contracts import (
    AgentFailure,
    AnalysisNotNullCheck,
    AnalysisPlanningDraft,
    AnalysisSourceConstraint,
)
from agent_runtime.conversation_context import plan_schema_dependencies
from agent_runtime.physical_references import PhysicalReferenceError, parse_physical_reference
from agent_runtime.query_protocol import preflight_sql_contract
from agent_runtime.runtime_limits import AgentRuntimeLimits
from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead


def _schema() -> SchemaSummaryRead:
    return SchemaSummaryRead(
        datasource_id="datasource_planning",
        dialect="duckdb",
        tables=[
            SchemaTableRead(
                name="orders",
                columns=[
                    SchemaColumnRead(name="order_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="customer_id", type="INTEGER", nullable=False),
                    SchemaColumnRead(name="order_date", type="DATE", nullable=False),
                    SchemaColumnRead(name="status", type="VARCHAR", nullable=False),
                ],
            )
        ],
    )


def _schema_with_ambiguous_order_id() -> SchemaSummaryRead:
    schema = _schema()
    return schema.model_copy(
        update={
            "tables": [
                *schema.tables,
                SchemaTableRead(
                    name="order_items",
                    columns=[
                        SchemaColumnRead(name="order_id", type="INTEGER", nullable=False),
                        SchemaColumnRead(name="product_id", type="INTEGER", nullable=False),
                    ],
                ),
            ]
        }
    )


def _schema_with_users() -> SchemaSummaryRead:
    schema = _schema()
    return schema.model_copy(
        update={
            "tables": [
                *schema.tables,
                SchemaTableRead(
                    name="users",
                    columns=[
                        SchemaColumnRead(name="id", type="INTEGER", nullable=False),
                    ],
                ),
            ]
        }
    )


def _order_count_plan(*, source_tables: list[str], aggregate_column: str) -> AnalysisPlanningDraft:
    return AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "统计订单总数",
                "acceptance_criteria": ["返回已核验的订单总数"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "统计订单总数",
                            "source_tables": source_tables,
                            "result_columns": ["order_count"],
                            "sql_constraints": [
                                {
                                    "kind": "aggregate",
                                    "function": "COUNT",
                                    "column": aggregate_column,
                                    "alias": "order_count",
                                }
                            ],
                            "claim_extractions": [
                                {
                                    "mode": "scalar",
                                    "name": "订单总数",
                                    "field": "order_count",
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )


def _monthly_plan(
    *, result_columns: list[str] | None = None, include_unknown_constraint: bool = False
) -> AnalysisPlanningDraft:
    return AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "按月份统计订单数",
                "acceptance_criteria": ["每个月都有已核验的订单数"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "读取月度订单数",
                            "source_tables": ["orders"],
                            "dimensions": ["order_month"],
                            "result_columns": result_columns or ["order_month"],
                            "sql_constraints": [
                                {"kind": "source", "table": "orders"},
                                {
                                    "kind": "aggregate",
                                    "function": "COUNT",
                                    "column": "*",
                                    "alias": "order_count",
                                },
                                {"kind": "group_by", "columns": ["order_month"]},
                            ]
                            + (
                                [{"kind": "column", "column": "missing_source"}]
                                if include_unknown_constraint
                                else []
                            ),
                            "claim_extractions": [
                                {
                                    "mode": "series",
                                    "name": "订单数",
                                    "value_field": "order_count",
                                    "dimension_fields": ["order_month"],
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )


def test_derived_result_column_is_allowed_only_when_declared_and_projected() -> None:
    materialized = materialize_analysis_plan(_monthly_plan(), _schema())

    assert not isinstance(materialized, AgentFailure)
    assertion = materialized.requirements[0].assertions[0]
    assert assertion.result_columns == ["order_month"]

    preflight = preflight_sql_contract(
        "SELECT strftime('%Y-%m', order_date) AS order_month, "
        "COUNT(*) AS order_count FROM orders GROUP BY order_month",
        dialect="duckdb",
        assertions=[assertion],
    )

    assert preflight.valid is True
    assert preflight.expected_columns == ["order_month", "order_count"]

    expression_grouped = preflight_sql_contract(
        "SELECT strftime('%Y-%m', order_date) AS order_month, "
        "COUNT(*) AS order_count FROM orders "
        "GROUP BY strftime('%Y-%m', order_date)",
        dialect="duckdb",
        assertions=[assertion],
    )

    assert expression_grouped.valid is True


def test_legacy_eq_null_filter_is_materialized_as_is_null() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "检查空客户编号",
                "acceptance_criteria": ["返回空值记录"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "过滤客户编号为空的记录",
                            "source_tables": ["orders"],
                            "result_columns": ["customer_id"],
                            "sql_constraints": [
                                {
                                    "kind": "filter",
                                    "column": "customer_id",
                                    "operator": "eq",
                                    "value": None,
                                }
                            ],
                            "claim_extractions": [
                                {
                                    "mode": "scalar",
                                    "name": "客户编号",
                                    "field": "customer_id",
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )
    materialized = materialize_analysis_plan(plan, _schema())

    assert not isinstance(materialized, AgentFailure)
    constraint = materialized.requirements[0].assertions[0].sql_constraints[0]
    assert constraint.operator == "is_null"


def test_plan_rejects_excessive_assertion_complexity() -> None:
    plan = _monthly_plan()
    original = plan.requirements[0].fulfillment.assertions[0]
    plan.requirements[0].fulfillment.assertions = [
        original.model_copy(update={"description": f"检查项 {index}"}) for index in range(5)
    ]

    result = materialize_analysis_plan(
        plan,
        _schema(),
        limits=AgentRuntimeLimits(max_assertions_per_requirement=4),
    )

    assert isinstance(result, AgentFailure)
    assert any(issue.error_type == "complexity_limit" for issue in result.validation_issues)


def test_unknown_physical_constraint_column_is_still_rejected() -> None:
    plan = _monthly_plan(include_unknown_constraint=True)
    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_qualified_physical_references_are_normalized_for_contract_checks() -> None:
    plan = _monthly_plan()
    assertion = plan.requirements[0].fulfillment.assertions[0]
    assertion.dimensions = ["orders.order_date"]
    assertion.sql_constraints[-1].columns = ["orders.order_date"]

    result = materialize_analysis_plan(plan, _schema())

    assert not isinstance(result, AgentFailure)


def test_bare_physical_column_is_scoped_to_assertion_source_tables() -> None:
    schema = _schema_with_ambiguous_order_id()
    plan = _order_count_plan(source_tables=["orders"], aggregate_column="order_id")
    result = materialize_analysis_plan(plan, schema)

    assert not isinstance(result, AgentFailure)
    required_tables, columns_by_table = plan_schema_dependencies(schema, plan)
    assert required_tables == {"orders"}
    assert columns_by_table == {"orders": {"order_id"}}
    preflight = preflight_sql_contract(
        "SELECT COUNT(order_id) AS order_count FROM orders",
        dialect="duckdb",
        assertions=[result.requirements[0].assertions[0]],
        schema=schema,
    )
    assert preflight.valid is True


def test_bare_physical_column_remains_ambiguous_across_allowed_source_tables() -> None:
    result = materialize_analysis_plan(
        _order_count_plan(
            source_tables=["orders", "order_items"],
            aggregate_column="order_id",
        ),
        _schema_with_ambiguous_order_id(),
    )

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_qualified_physical_column_selects_one_allowed_source_table() -> None:
    schema = _schema_with_ambiguous_order_id()
    result = materialize_analysis_plan(
        _order_count_plan(
            source_tables=["orders", "order_items"],
            aggregate_column="orders.order_id",
        ),
        schema,
    )

    assert not isinstance(result, AgentFailure)
    preflight = preflight_sql_contract(
        "SELECT COUNT(orders.order_id) AS order_count "
        "FROM orders JOIN order_items ON orders.order_id = order_items.order_id",
        dialect="duckdb",
        assertions=[result.requirements[0].assertions[0]],
        schema=schema,
    )
    assert preflight.valid is True


def test_dimension_must_belong_to_assertion_source_tables() -> None:
    plan = _monthly_plan()
    assertion = plan.requirements[0].fulfillment.assertions[0]
    assertion.dimensions = ["users.id"]

    result = materialize_analysis_plan(plan, _schema_with_users())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_qualified_dimension_cannot_be_smuggled_in_as_a_result_alias() -> None:
    plan = _monthly_plan()
    assertion = plan.requirements[0].fulfillment.assertions[0]
    assertion.dimensions = ["users.id"]
    assertion.result_columns = ["users.id", "order_month"]

    result = materialize_analysis_plan(plan, _schema_with_users())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_source_constraint_must_match_assertion_source_tables() -> None:
    plan = _monthly_plan()
    assertion = plan.requirements[0].fulfillment.assertions[0]
    assertion.sql_constraints[0] = AnalysisSourceConstraint(kind="source", table="users")

    result = materialize_analysis_plan(plan, _schema_with_users())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_source_constraint_requires_explicit_assertion_source_tables() -> None:
    plan = _order_count_plan(source_tables=[], aggregate_column="order_id")
    assertion = plan.requirements[0].fulfillment.assertions[0]
    assertion.sql_constraints.append(AnalysisSourceConstraint(kind="source", table="orders"))

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    issue = next(
        item for item in result.validation_issues if item.path.endswith("sql_constraints[1].table")
    )
    assert issue.error_type == "source_scope_conflict"
    assert issue.repair_reason == "source_scope_conflict"
    assert issue.actual == "orders"
    assert "source_tables" in issue.expected


def test_physical_reference_reports_disallowed_table_and_unknown_column_separately() -> None:
    schema = _schema_with_users()

    with pytest.raises(PhysicalReferenceError) as disallowed:
        parse_physical_reference(schema, "users", require_column=False, allowed_tables=["orders"])
    assert disallowed.value.reason_code == "table_not_allowed"
    assert disallowed.value.safe_value == "users"

    with pytest.raises(PhysicalReferenceError) as unknown:
        parse_physical_reference(schema, "orders.missing", allowed_tables=["orders"])
    assert unknown.value.reason_code == "unknown_column"
    assert unknown.value.table == "orders"
    assert unknown.value.column == "missing"
    assert unknown.value.candidates
    assert all("." in candidate for candidate in unknown.value.candidates)


def test_physical_reference_diagnostic_does_not_echo_sql_or_paths() -> None:
    schema = _schema_with_users()

    with pytest.raises(PhysicalReferenceError) as error:
        parse_physical_reference(schema, "SELECT * FROM users")

    assert error.value.safe_value == ""
    assert "SELECT" not in str(error.value)


def test_plan_finding_actual_does_not_echo_untrusted_source_identifier() -> None:
    plan = _order_count_plan(source_tables=["password=do-not-echo"], aggregate_column="order_id")

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    rendered = result.model_dump_json()
    assert "password=do-not-echo" not in rendered
    assert result.validation_issues[0].actual == "未提供"


def test_non_group_constraint_finding_uses_the_constraint_column_path() -> None:
    plan = _order_count_plan(source_tables=["orders"], aggregate_column="missing")

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    issue = next(item for item in result.validation_issues if item.error_type == "unknown_column")
    assert issue.path.endswith("sql_constraints[0].column")


def test_duplicate_source_tables_are_rejected() -> None:
    result = materialize_analysis_plan(
        _order_count_plan(source_tables=["orders", "orders"], aggregate_column="order_id"),
        _schema(),
    )

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_source_and_field_matching_is_case_insensitive_and_accepts_quoted_names() -> None:
    result = materialize_analysis_plan(
        _order_count_plan(
            source_tables=['"ORDERS"'],
            aggregate_column='"ORDERS"."ORDER_ID"',
        ),
        _schema(),
    )

    assert not isinstance(result, AgentFailure)


def test_empty_source_tables_use_unique_frozen_schema_resolution() -> None:
    result = materialize_analysis_plan(
        _order_count_plan(source_tables=[], aggregate_column="order_id"),
        _schema(),
    )

    assert not isinstance(result, AgentFailure)


def test_claim_physical_field_must_belong_to_assertion_source_tables() -> None:
    plan = _order_count_plan(source_tables=["orders"], aggregate_column="order_id")
    extraction = plan.requirements[0].fulfillment.assertions[0].claim_extractions[0]
    extraction.field = "users.id"

    result = materialize_analysis_plan(plan, _schema_with_users())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_bare_claim_field_from_another_table_is_reported_as_source_scope_conflict() -> None:
    plan = _order_count_plan(source_tables=["orders"], aggregate_column="order_id")
    extraction = plan.requirements[0].fulfillment.assertions[0].claim_extractions[0]
    extraction.field = "id"

    result = materialize_analysis_plan(plan, _schema_with_users())

    assert isinstance(result, AgentFailure)
    issue = next(
        item
        for item in result.validation_issues
        if item.path.endswith("claim_extractions[0].field")
    )
    assert issue.error_type == "source_scope_conflict"
    assert issue.repair_reason == "source_scope_conflict"
    assert issue.actual == "id"
    assert "users.id" in issue.expected


def test_result_check_physical_field_must_belong_to_assertion_source_tables() -> None:
    plan = _order_count_plan(source_tables=["orders"], aggregate_column="order_id")
    assertion = plan.requirements[0].fulfillment.assertions[0]
    assertion.result_checks = [
        AnalysisNotNullCheck(kind="not_null", required=True, fields=["users.id"])
    ]

    result = materialize_analysis_plan(plan, _schema_with_users())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_plan_failure_contains_safe_opening_repair_diagnostic() -> None:
    plan = _monthly_plan(include_unknown_constraint=True)

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    assert result.validation_issues[0].error_type == "unknown_column"
    assert result.validation_issues[0].repair_reason == "schema_reference_invalid"
    assert result.validation_issues[0].actual == "missing_source"
    assert result.validation_issues[0].path.startswith("requirements[0]")
    assert "当前 assertion.source_tables" in result.validation_issues[0].action


def test_discovery_rejects_formal_evidence_targets() -> None:
    with pytest.raises(ValueError, match="discovery plan cannot contain evidence"):
        AnalysisPlanningDraft(
            mode="discovery",
            discovery_scope={
                "tables": ["orders"],
                "columns": ["orders.id"],
            },
            requirements=[
                {
                    "description": "统计订单数",
                    "acceptance_criteria": ["返回订单数"],
                    "fulfillment": {
                        "mode": "evidence",
                        "assertions": [
                            {
                                "description": "读取订单数",
                                "source_tables": ["orders"],
                                "result_columns": ["order_count"],
                                "claim_extractions": [
                                    {
                                        "mode": "scalar",
                                        "name": "订单数",
                                        "field": "order_count",
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        )


def test_chart_is_an_artifact_requirement_not_a_claim_result_column() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "生成订单图表",
                "acceptance_criteria": ["登记一张图表产物"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "读取订单数",
                            "source_tables": ["orders"],
                            "result_columns": ["chart"],
                            "claim_extractions": [
                                {
                                    "mode": "scalar",
                                    "name": "chart",
                                    "field": "chart",
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
        execution_constraints={
            "required_artifacts": [
                {"kind": "chart", "minimum_count": 1, "description": "订单趋势图"}
            ]
        },
    )

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"


def test_series_without_aggregate_or_group_by_is_rejected() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "查看表之间的连接键",
                "acceptance_criteria": ["列出可核验的连接关系"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "读取连接键明细",
                            "source_tables": ["orders"],
                            "result_columns": ["order_id", "customer_id"],
                            "claim_extractions": [
                                {
                                    "mode": "series",
                                    "name": "连接键",
                                    "value_field": "order_id",
                                    "dimension_fields": ["customer_id"],
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    issue = next(item for item in result.validation_issues if item.error_type == "shape_invalid")
    assert issue.path == "requirements[0].fulfillment.assertions[0].claim_extractions[0]"
    assert issue.repair_reason == "shape_invalid"
    assert issue.actual == "series 缺少 aggregate 或 group_by"
    assert "context_only" in issue.action


def test_series_with_group_by_or_aggregate_still_materializes() -> None:
    grouped = materialize_analysis_plan(_monthly_plan(), _schema())
    aggregate_only = materialize_analysis_plan(
        AnalysisPlanningDraft(
            mode="ready",
            requirements=[
                {
                    "description": "按状态统计订单数",
                    "acceptance_criteria": ["每个状态都有已核验的订单数"],
                    "fulfillment": {
                        "mode": "evidence",
                        "assertions": [
                            {
                                "description": "按状态计数",
                                "source_tables": ["orders"],
                                "dimensions": ["status"],
                                "result_columns": ["status", "order_count"],
                                "sql_constraints": [
                                    {
                                        "kind": "aggregate",
                                        "function": "COUNT",
                                        "column": "*",
                                        "alias": "order_count",
                                    }
                                ],
                                "claim_extractions": [
                                    {
                                        "mode": "series",
                                        "name": "状态订单数",
                                        "value_field": "order_count",
                                        "dimension_fields": ["status"],
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        ),
        _schema(),
    )

    assert not isinstance(grouped, AgentFailure)
    assert not isinstance(aggregate_only, AgentFailure)


def test_context_only_plan_cannot_require_formal_artifact() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "说明当前表结构",
                "acceptance_criteria": ["只依据冻结 Schema 回答"],
                "fulfillment": {"mode": "context_only", "sources": ["schema"]},
            }
        ],
        execution_constraints={
            "required_artifacts": [
                {"kind": "markdown", "description": "可下载报告"},
            ]
        },
    )

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    assert result.code.value == "ANALYSIS_PLAN_INVALID"
    issue = result.validation_issues[0]
    assert issue.path == "execution_constraints.required_artifacts[0]"
    assert issue.repair_reason == "artifact_conflict"
    assert issue.actual == "没有 evidence fulfillment"
    assert "evidence fulfillment" in issue.expected
    assert "不能用没有数据证据" in issue.rule
    assert "删除 required_artifacts" in issue.action


def test_blocked_only_plan_cannot_require_formal_artifact() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "导出当前数据报告",
                "acceptance_criteria": ["登记报告产物"],
                "fulfillment": {
                    "mode": "blocked",
                    "reason": "capability_unavailable",
                    "detail": "当前没有可用的导出能力",
                },
            }
        ],
        execution_constraints={
            "required_artifacts": [
                {"kind": "markdown", "description": "可下载报告"},
            ]
        },
    )

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    issue = next(
        item for item in result.validation_issues if item.error_type == "artifact_conflict"
    )
    assert issue.actual == "没有 evidence fulfillment"
    assert issue.repair_reason == "artifact_conflict"


def test_evidence_plan_cannot_forbid_sql_tool() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "统计订单数",
                "acceptance_criteria": ["返回订单数"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "读取订单数",
                            "source_tables": ["orders"],
                            "result_columns": ["order_count"],
                            "claim_extractions": [
                                {
                                    "mode": "scalar",
                                    "name": "订单数",
                                    "field": "order_count",
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
        execution_constraints={"forbidden_tools": ["run_sql_readonly"]},
    )

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    issue = next(
        item for item in result.validation_issues if item.error_type == "capability_conflict"
    )
    assert issue.path == "execution_constraints.forbidden_tools"
    assert issue.actual == "run_sql_readonly"
    assert "run_sql_readonly" in issue.expected


def test_required_artifact_cannot_forbid_python_tool() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "统计订单数并交付图表",
                "acceptance_criteria": ["返回订单数"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "读取订单数",
                            "source_tables": ["orders"],
                            "result_columns": ["order_count"],
                            "claim_extractions": [
                                {
                                    "mode": "scalar",
                                    "name": "订单数",
                                    "field": "order_count",
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
        execution_constraints={
            "forbidden_tools": ["run_python"],
            "required_artifacts": [
                {"kind": "chart", "description": "订单图表"},
            ],
        },
    )

    result = materialize_analysis_plan(plan, _schema())

    assert isinstance(result, AgentFailure)
    issue = next(
        item for item in result.validation_issues if item.error_type == "capability_conflict"
    )
    assert issue.actual == "run_python"
    assert "run_python" in issue.expected


def test_independent_assertions_keep_minimal_contracts_separate() -> None:
    plan = AnalysisPlanningDraft(
        mode="ready",
        requirements=[
            {
                "description": "按状态统计订单数",
                "acceptance_criteria": ["每个状态都有订单数"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "按状态统计订单数",
                            "source_tables": ["orders"],
                            "dimensions": ["status"],
                            "result_columns": ["order_count"],
                            "sql_constraints": [
                                {"kind": "source", "table": "orders"},
                                {
                                    "kind": "aggregate",
                                    "function": "COUNT",
                                    "column": "order_id",
                                    "alias": "order_count",
                                },
                                {"kind": "group_by", "columns": ["status"]},
                            ],
                            "claim_extractions": [
                                {
                                    "mode": "series",
                                    "name": "订单数",
                                    "value_field": "order_count",
                                    "dimension_fields": ["status"],
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "description": "统计客户订单数",
                "acceptance_criteria": ["返回客户订单数"],
                "fulfillment": {
                    "mode": "evidence",
                    "assertions": [
                        {
                            "description": "统计客户订单数",
                            "source_tables": ["orders"],
                            "result_columns": ["customer_count"],
                            "sql_constraints": [
                                {"kind": "source", "table": "orders"},
                                {
                                    "kind": "aggregate",
                                    "function": "COUNT",
                                    "column": "customer_id",
                                    "alias": "customer_count",
                                },
                            ],
                            "claim_extractions": [
                                {
                                    "mode": "scalar",
                                    "name": "客户订单数",
                                    "field": "customer_count",
                                    "required": True,
                                }
                            ],
                        }
                    ],
                },
            },
        ],
    )

    result = materialize_analysis_plan(plan, _schema())

    assert not isinstance(result, AgentFailure)
    assert [item.id for item in result.requirements[0].assertions] == ["R1.A1"]
    assert [item.id for item in result.requirements[1].assertions] == ["R2.A1"]
    assert result.requirements[0].assertions[0].dimensions == ["status"]
    assert result.requirements[0].assertions[0].sql_constraints[1].column == "order_id"
    assert result.requirements[1].assertions[0].sql_constraints[1].column == "customer_id"
