# 09-28-opening-protocol-derivation 大切换（prd R4、design §5）测试删留说明：
# result_verifier 不再执行任何预言式结果检查，以下行为及其测试被有意移除：
#   - non_empty / row_count / not_null / unique / equals / sum / comparison /
#     budget_conservation / ratio / column_sum_equals 十类检查的执行与拒绝
#     （test_result_verifier_rejects_each_required_check_kind、
#      test_result_verifier_checks_column_sum_equals、九类检查全通过用例的检查部分）
# 空结果与 NULL 的行为：0 行 → series 为空、required scalar 无值时如实产出
# RESULT_CLAIM_VALUE_MISSING（值确实没拿到，不能编造）；值提取、series 维度
# 完整性、去重、max_items 与 RESULT_VERIFIED_VALUE_LIMIT 上限全部保留。
# 断路器所需的 RESULT_EXPECTED_COLUMN_MISSING assertion 归因新增专属测试。
from __future__ import annotations

from agent_runtime.contracts import AnalysisAssertion
from agent_runtime.result_verifier import verify_analysis_result
from contracts.datasources import TableDataRead


def _assertion(
    *,
    claim_extractions: list[dict[str, object]] | None = None,
) -> AnalysisAssertion:
    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="验证查询结果",
        claim_extractions=claim_extractions
        or [{"mode": "scalar", "name": "value", "field": "value", "required": False}],
    )


def test_result_verifier_extracts_claim_values_without_prediction_checks() -> None:
    result = TableDataRead(
        columns=["city", "total"],
        rows=[["杭州", 7]],
        row_count=1,
    )
    assertion = _assertion(
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "sales_total",
                "field": "total",
                "unit": "CNY",
                "required": True,
            }
        ],
    )

    verification = verify_analysis_result(result, [assertion], expected_columns=["city", "total"])

    assert verification.valid is True
    assert verification.findings == []
    assert verification.verified_values[0].model_dump() == {
        "name": "sales_total",
        "value": 7,
        "unit": "CNY",
        "tolerance": 0,
        "assertion_id": "R1.A1",
        "fact_key": "total",
        "dimensions": {},
    }


def test_result_verifier_reports_missing_value_on_empty_result_as_missing_claim_value() -> None:
    """q14：空结果如实返回；required scalar 没拿到值时报告缺失，不编造数值。"""

    assertion = _assertion(
        claim_extractions=[
            {"mode": "scalar", "name": "optional_total", "field": "total", "required": False}
        ]
    )
    required_assertion = _assertion(
        claim_extractions=[{"mode": "scalar", "name": "total", "field": "total", "required": True}]
    )

    optional = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[], row_count=0),
        [assertion],
    )
    required = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[], row_count=0),
        [required_assertion],
    )

    assert optional.valid is True
    assert optional.verified_values == []
    assert required.valid is False
    assert [item.code for item in required.findings] == ["RESULT_CLAIM_VALUE_MISSING:total"]


def test_result_verifier_extracts_null_value_as_none_instead_of_missing() -> None:
    """q08：零匹配行的 SUM 返回 NULL 时，值如实提取为 None，不判死。"""

    assertion = _assertion(
        claim_extractions=[{"mode": "scalar", "name": "total", "field": "total", "required": True}]
    )

    verification = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[[None]], row_count=1),
        [assertion],
    )

    assert verification.valid is True
    assert [item.value for item in verification.verified_values] == [None]


def test_result_verifier_rejects_missing_expected_column() -> None:
    missing_column = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[[1]], row_count=1),
        [_assertion()],
        expected_columns=["missing"],
    )

    assert missing_column.valid is False
    assert [item.code for item in missing_column.findings] == [
        "RESULT_EXPECTED_COLUMN_MISSING:missing"
    ]


def test_result_verifier_attributes_missing_column_to_referencing_assertion() -> None:
    """缺少的结果列归因到声明/引用它的 assertion，供断路器按检查项统计。"""

    claimer = AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="引用缺失列的检查项",
        result_columns=["region", "avg_order_amount"],
        claim_extractions=[
            {"mode": "scalar", "name": "均值", "field": "avg_order_amount", "required": True}
        ],
    )
    other = AnalysisAssertion(
        id="R1.A2",
        requirement_id="R1",
        description="无关检查项",
        result_columns=["city_total"],
        claim_extractions=[
            {"mode": "scalar", "name": "总额", "field": "city_total", "required": True}
        ],
    )

    verification = verify_analysis_result(
        TableDataRead(columns=["region", "city_total"], rows=[["华东", 10]], row_count=1),
        [other, claimer],
        expected_columns=["region", "avg_order_amount"],
    )

    missing = [item for item in verification.findings if item.code.startswith("RESULT_EXPECTED")]
    assert [(item.code, item.assertion_id) for item in missing] == [
        ("RESULT_EXPECTED_COLUMN_MISSING:avg_order_amount", "R1.A1")
    ]


def test_result_verifier_expands_a_series_with_stable_dimensions() -> None:
    verification = verify_analysis_result(
        TableDataRead(
            columns=["city", "total"],
            rows=[["杭州", 10], ["上海", 20]],
            row_count=2,
        ),
        [
            _assertion(
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "city_total",
                        "value_field": "total",
                        "dimension_fields": ["city"],
                    }
                ]
            )
        ],
    )

    assert verification.valid is True
    assert [(item.value, item.dimensions) for item in verification.verified_values] == [
        (10, {"city": "杭州"}),
        (20, {"city": "上海"}),
    ]


def test_result_verifier_accepts_series_above_the_legacy_64_fact_limit() -> None:
    rows = [[f"bucket-{index:03d}", index] for index in range(72)]

    verification = verify_analysis_result(
        TableDataRead(columns=["bucket", "total"], rows=rows, row_count=len(rows)),
        [
            _assertion(
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "bucket_total",
                        "value_field": "total",
                        "dimension_fields": ["bucket"],
                    }
                ]
            )
        ],
    )

    assert verification.valid is True
    assert len(verification.verified_values) == 72


def test_result_verifier_rejects_combined_extractions_above_the_fact_limit() -> None:
    rows = [[f"bucket-{index:03d}", index, index + 1] for index in range(251)]

    verification = verify_analysis_result(
        TableDataRead(
            columns=["bucket", "first_total", "second_total"], rows=rows, row_count=len(rows)
        ),
        [
            _assertion(
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "first_total",
                        "value_field": "first_total",
                        "dimension_fields": ["bucket"],
                        "max_items": 500,
                    },
                    {
                        "mode": "series",
                        "name": "second_total",
                        "value_field": "second_total",
                        "dimension_fields": ["bucket"],
                        "max_items": 500,
                    },
                ]
            )
        ],
    )

    assert verification.valid is False
    assert verification.verified_values == []
    assert verification.findings[-1].code == "RESULT_VERIFIED_VALUE_LIMIT_EXCEEDED"


def test_result_verifier_rejects_duplicate_or_oversized_series() -> None:
    duplicate = verify_analysis_result(
        TableDataRead(columns=["city", "total"], rows=[["杭州", 10], ["杭州", 20]], row_count=2),
        [
            _assertion(
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "city_total",
                        "value_field": "total",
                        "dimension_fields": ["city"],
                    }
                ]
            )
        ],
    )
    oversized = verify_analysis_result(
        TableDataRead(columns=["city", "total"], rows=[["杭州", 10], ["上海", 20]], row_count=2),
        [
            _assertion(
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "city_total",
                        "value_field": "total",
                        "dimension_fields": ["city"],
                        "max_items": 1,
                    }
                ]
            )
        ],
    )

    assert duplicate.valid is False
    assert duplicate.findings[0].code == "RESULT_SERIES_DIMENSIONS_DUPLICATE:city_total"
    assert oversized.valid is False
    assert oversized.findings[0].code == "RESULT_SERIES_MAX_ITEMS_EXCEEDED:city_total"


def test_result_verifier_rejects_series_row_missing_required_value_or_dimension() -> None:
    """series 行缺少值或维度字段时如实拒绝，不能产出半条事实。"""

    base = {
        "mode": "series",
        "name": "city_total",
        "value_field": "total",
        "dimension_fields": ["city"],
    }
    assertion = _assertion(claim_extractions=[base])

    null_value = verify_analysis_result(
        TableDataRead(columns=["city", "total"], rows=[["杭州", None]], row_count=1),
        [assertion],
    )
    missing_dimension = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[[10]], row_count=1),
        [assertion],
    )

    assert null_value.valid is False
    assert null_value.findings[0].code == "RESULT_SERIES_VALUE_MISSING:city_total"
    assert missing_dimension.valid is False
    assert missing_dimension.findings[0].code == "RESULT_SERIES_VALUE_MISSING:city_total"
