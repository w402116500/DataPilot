"""Agent Runtime 内部契约的形状测试。

09-28-opening-protocol-derivation 大切换（prd R4）：AnalysisSqlConstraint /
AnalysisResultCheck 两族类型及 assertion 上的 sql_constraints / result_checks
字段被整体删除，原
test_result_checks_are_discriminated_and_allow_null_literal_operands 中针对
检查联合类型的判别测试随之移除；其中 AnalysisValueOperand 的
"operand requires field or literal" 校验仍存在，移植为
test_value_operand_requires_field_or_literal。
"""

from __future__ import annotations

import pytest
from agent_runtime.contracts import (
    AnalysisAssertionDraft,
    AnalysisEvidenceBinding,
    AnalysisExecutionConstraints,
    AnalysisQueryAttempt,
    AnalysisRequirement,
    AnalysisValueOperand,
    create_analysis_assertions,
)
from contracts.datalink import DataLinkColumnProfileRead, DataLinkSemanticField
from pydantic import ValidationError


def test_analysis_assertions_use_server_owned_ids_and_preserve_contract_fields() -> None:
    assertions = create_analysis_assertions(
        "R3",
        [
            AnalysisAssertionDraft(
                description="统计城市销售额",
                source_tables=["orders"],
                dimensions=["city"],
                result_columns=["city", "sales_total"],
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
    assert assertions[0].result_columns == ["city", "sales_total"]
    assert assertions[0].dimensions == ["city"]


def test_value_operand_requires_field_or_literal() -> None:
    with pytest.raises(ValidationError, match="an operand requires field or literal"):
        AnalysisValueOperand()


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


def test_datalink_profile_and_semantic_field_reject_oversized_typical_values() -> None:
    """典型取值的合同上限：每列最多 20 个值，单值最长 200 字符。"""

    profile_base = {
        "column_id": "column:ds:orders:status",
        "dtype": "text",
        "null_rate": 0.0,
        "distinct_count": 20,
        "unique_rate": 1.0,
    }
    field_base = {"table": "orders", "column": "status"}

    accepted = [f"value-{index}" for index in range(20)]
    assert DataLinkColumnProfileRead(**profile_base, typical_values=accepted).typical_values == (
        accepted
    )
    assert DataLinkSemanticField(**field_base, typical_values=accepted).typical_values == accepted

    with pytest.raises(ValidationError):
        DataLinkColumnProfileRead(
            **profile_base, typical_values=[f"value-{index}" for index in range(21)]
        )
    with pytest.raises(ValidationError):
        DataLinkSemanticField(
            **field_base, typical_values=[f"value-{index}" for index in range(21)]
        )
    with pytest.raises(ValidationError):
        DataLinkColumnProfileRead(**profile_base, typical_values=["x" * 201])
    with pytest.raises(ValidationError):
        DataLinkSemanticField(**field_base, typical_values=["x" * 201])
