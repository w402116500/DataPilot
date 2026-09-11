from __future__ import annotations

import pytest
from agent_runtime.contracts import (
    AnalysisAssertionDraft,
    AnalysisComparisonCheck,
    AnalysisEvidenceBinding,
    AnalysisExecutionConstraints,
    AnalysisNotNullCheck,
    AnalysisQueryAttempt,
    AnalysisRequirement,
    AnalysisResultCheck,
    AnalysisRowCountCheck,
    AnalysisUniqueCheck,
    create_analysis_assertions,
)
from pydantic import ValidationError


def test_analysis_assertions_use_server_owned_ids_and_preserve_contract_fields() -> None:
    assertions = create_analysis_assertions(
        "R3",
        [
            AnalysisAssertionDraft(
                description="统计城市销售额",
                source_tables=["orders"],
                dimensions=["city"],
                sql_constraints=[
                    {
                        "kind": "aggregate",
                        "function": "SUM",
                        "column": "amount",
                        "alias": "sales_total",
                    },
                    {"kind": "group_by", "columns": ["city"]},
                ],
                result_checks=[
                    {"kind": "non_empty", "required": True},
                    {"kind": "not_null", "required": True, "fields": ["city", "sales_total"]},
                ],
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "sales_total",
                        "value_field": "sales_total",
                        "dimension_fields": ["city"],
                    }
                ],
            ),
            AnalysisAssertionDraft(
                description="比较城市销售额",
                claim_extractions=[
                    {
                        "mode": "scalar",
                        "name": "top_city",
                        "field": "city",
                        "required": True,
                    }
                ],
            ),
        ],
    )

    assert [item.id for item in assertions] == ["R3.A1", "R3.A2"]
    assert assertions[0].requirement_id == "R3"
    assert assertions[0].sql_constraints[0].kind == "aggregate"
    assert assertions[0].result_checks[1].kind == "not_null"


def test_result_checks_are_discriminated_and_allow_null_literal_operands() -> None:
    checks: list[AnalysisResultCheck] = [
        AnalysisRowCountCheck(kind="row_count", required=True, min=1),
        AnalysisNotNullCheck(kind="not_null", required=True, fields=["city"]),
        AnalysisUniqueCheck(kind="unique", required=False, fields=["city"]),
        AnalysisComparisonCheck(
            kind="comparison",
            required=True,
            left={"field": "sales_total"},
            right={"literal": 0},
            operator="gte",
        ),
    ]

    assert [check.kind for check in checks] == ["row_count", "not_null", "unique", "comparison"]

    with pytest.raises(ValidationError, match="an operand requires field or literal"):
        AnalysisComparisonCheck(
            kind="comparison",
            required=True,
            left={},
            right={"literal": 0},
            operator="gte",
        )


def test_requirement_and_query_evidence_records_have_full_runtime_shape() -> None:
    requirement = AnalysisRequirement(
        id="R1",
        description="统计订单量",
        acceptance_criteria=["结果非空"],
        assertions=create_analysis_assertions(
            "R1",
            [
                AnalysisAssertionDraft(
                    description="统计订单量",
                    claim_extractions=[
                        {
                            "mode": "scalar",
                            "name": "order_count",
                            "field": "order_count",
                            "required": True,
                        }
                    ],
                )
            ],
        ),
        query_attempt_ids=["Q1"],
    )
    query = AnalysisQueryAttempt(
        id="Q1",
        requirement_ids=[requirement.id],
        assertion_ids=["R1.A1"],
        assertions=requirement.assertions,
        expected_columns=["order_count"],
    )
    binding = AnalysisEvidenceBinding(
        id="E1",
        requirement_id="R1",
        query_attempt_id="Q1",
        artifact_id="artifact_1",
        audit_log_id="audit_1",
        result_fields=["order_count"],
    )

    assert query.requirement_ids == ["R1"]
    assert query.assertion_ids == ["R1.A1"]
    assert binding.validation_status == "passed"


def test_assertion_draft_rejects_unknown_fields_in_strict_models() -> None:
    with pytest.raises(ValidationError):
        AnalysisAssertionDraft(
            description="订单量",
            claim_extractions=[
                {"mode": "scalar", "name": "count", "field": "count", "required": True}
            ],
            unexpected="not allowed",
        )


def test_execution_constraints_allow_multiple_distinct_required_artifacts() -> None:
    constraints = AnalysisExecutionConstraints(
        required_artifacts=[
            {"kind": "chart", "description": "订单趋势图"},
            {"kind": "chart", "description": "事件趋势图"},
            {"kind": "chart", "description": "客户分布图"},
            {"kind": "markdown", "description": "分析报告"},
        ]
    )

    assert len(constraints.required_artifacts) == 4

    with pytest.raises(ValidationError):
        AnalysisExecutionConstraints(
            required_artifacts=[
                {"kind": "chart", "description": f"图表 {index}"} for index in range(9)
            ]
        )
