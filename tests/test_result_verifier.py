from __future__ import annotations

import pytest
from agent_runtime.contracts import AnalysisAssertion
from agent_runtime.result_verifier import verify_analysis_result
from contracts.datasources import TableDataRead


def _assertion(
    *,
    checks: list[dict[str, object]],
    claim_extractions: list[dict[str, object]] | None = None,
) -> AnalysisAssertion:
    return AnalysisAssertion(
        id="R1.A1",
        requirement_id="R1",
        description="验证查询结果",
        result_checks=checks,
        claim_extractions=claim_extractions
        or [{"mode": "scalar", "name": "value", "field": "value", "required": False}],
    )


def test_result_verifier_accepts_all_nine_check_kinds_and_extracts_claim_values() -> None:
    result = TableDataRead(
        columns=[
            "city",
            "left",
            "right",
            "total",
            "part_one",
            "part_two",
            "ratio",
            "numerator",
            "denominator",
        ],
        rows=[["杭州", 10, 10, 7, 3, 4, 0.5, 1, 2]],
        row_count=1,
    )
    assertion = _assertion(
        checks=[
            {"kind": "non_empty", "required": True},
            {"kind": "row_count", "required": True, "min": 1, "max": 1},
            {"kind": "not_null", "required": True, "fields": ["city", "total"]},
            {"kind": "unique", "required": True, "fields": ["city"]},
            {
                "kind": "equals",
                "required": True,
                "left": {"field": "left"},
                "right": {"field": "right"},
            },
            {
                "kind": "sum",
                "required": True,
                "total": {"field": "total"},
                "parts": [{"field": "part_one"}, {"field": "part_two"}],
            },
            {
                "kind": "comparison",
                "required": True,
                "left": {"field": "total"},
                "right": {"field": "part_one"},
                "operator": "gt",
            },
            {
                "kind": "budget_conservation",
                "required": True,
                "left": {"field": "left"},
                "right": {"field": "right"},
            },
            {
                "kind": "ratio",
                "required": True,
                "value": {"field": "ratio"},
                "numerator": {"field": "numerator"},
                "denominator": {"field": "denominator"},
            },
        ],
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


@pytest.mark.parametrize(
    ("result", "check", "expected_code"),
    [
        (
            TableDataRead(columns=["value"], rows=[], row_count=0),
            {"kind": "non_empty", "required": True},
            "RESULT_CHECK_NON_EMPTY_FAILED",
        ),
        (
            TableDataRead(columns=["value"], rows=[[1]], row_count=1),
            {"kind": "row_count", "required": True, "min": 2},
            "RESULT_CHECK_ROW_COUNT_FAILED",
        ),
        (
            TableDataRead(columns=["value"], rows=[[None]], row_count=1),
            {"kind": "not_null", "required": True, "fields": ["value"]},
            "RESULT_CHECK_NOT_NULL_FAILED:value",
        ),
        (
            TableDataRead(columns=["city"], rows=[["杭州"], ["杭州"]], row_count=2),
            {"kind": "unique", "required": True, "fields": ["city"]},
            "RESULT_CHECK_UNIQUE_FAILED:city",
        ),
        (
            TableDataRead(columns=["left", "right"], rows=[[1, 2]], row_count=1),
            {
                "kind": "equals",
                "required": True,
                "left": {"field": "left"},
                "right": {"field": "right"},
            },
            "RESULT_CHECK_EQUALS_FAILED",
        ),
        (
            TableDataRead(columns=["total", "part_one", "part_two"], rows=[[5, 1, 2]], row_count=1),
            {
                "kind": "sum",
                "required": True,
                "total": {"field": "total"},
                "parts": [{"field": "part_one"}, {"field": "part_two"}],
            },
            "RESULT_CHECK_SUM_FAILED",
        ),
        (
            TableDataRead(columns=["left", "right"], rows=[[1, 2]], row_count=1),
            {
                "kind": "comparison",
                "required": True,
                "left": {"field": "left"},
                "right": {"field": "right"},
                "operator": "gt",
            },
            "RESULT_CHECK_COMPARISON_FAILED:gt",
        ),
        (
            TableDataRead(columns=["left", "right"], rows=[[1, 2]], row_count=1),
            {
                "kind": "budget_conservation",
                "required": True,
                "left": {"field": "left"},
                "right": {"field": "right"},
            },
            "RESULT_CHECK_BUDGET_CONSERVATION_FAILED",
        ),
        (
            TableDataRead(
                columns=["value", "numerator", "denominator"], rows=[[0.6, 1, 2]], row_count=1
            ),
            {
                "kind": "ratio",
                "required": True,
                "value": {"field": "value"},
                "numerator": {"field": "numerator"},
                "denominator": {"field": "denominator"},
            },
            "RESULT_CHECK_RATIO_FAILED",
        ),
    ],
)
def test_result_verifier_rejects_each_required_check_kind(
    result: TableDataRead,
    check: dict[str, object],
    expected_code: str,
) -> None:
    verification = verify_analysis_result(result, [_assertion(checks=[check])])

    assert verification.valid is False
    assert [finding.code for finding in verification.findings] == [expected_code]
    assert verification.verified_values == []


def test_result_verifier_keeps_warning_checks_but_rejects_missing_expected_column() -> None:
    assertion = _assertion(
        checks=[{"kind": "non_empty", "required": False}],
        claim_extractions=[
            {
                "mode": "scalar",
                "name": "optional_total",
                "field": "total",
                "required": False,
            }
        ],
    )

    warning_only = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[], row_count=0),
        [assertion],
    )
    missing_column = verify_analysis_result(
        TableDataRead(columns=["total"], rows=[[1]], row_count=1),
        [_assertion(checks=[])],
        expected_columns=["missing"],
    )

    assert warning_only.valid is True
    assert [(item.code, item.severity) for item in warning_only.findings] == [
        ("RESULT_CHECK_NON_EMPTY_FAILED", "warning")
    ]
    assert warning_only.verified_values == []
    assert missing_column.valid is False
    assert [item.code for item in missing_column.findings] == [
        "RESULT_EXPECTED_COLUMN_MISSING:missing"
    ]


def test_result_verifier_distinguishes_null_literal_from_missing_value() -> None:
    assertion = _assertion(
        checks=[
            {
                "kind": "equals",
                "required": True,
                "left": {"field": "value"},
                "right": {"literal": None},
            }
        ]
    )

    verification = verify_analysis_result(
        TableDataRead(columns=["value"], rows=[[None]], row_count=1),
        [assertion],
    )

    assert verification.valid is True


def test_result_verifier_expands_a_series_with_stable_dimensions() -> None:
    verification = verify_analysis_result(
        TableDataRead(
            columns=["city", "total"],
            rows=[["杭州", 10], ["上海", 20]],
            row_count=2,
        ),
        [
            _assertion(
                checks=[{"kind": "unique", "required": True, "fields": ["city"]}],
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "city_total",
                        "value_field": "total",
                        "dimension_fields": ["city"],
                    }
                ],
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
                checks=[{"kind": "unique", "required": True, "fields": ["bucket"]}],
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "bucket_total",
                        "value_field": "total",
                        "dimension_fields": ["bucket"],
                    }
                ],
            )
        ],
    )

    assert verification.valid is True
    assert len(verification.verified_values) == 72


def test_result_verifier_rejects_combined_extractions_above_the_fact_limit() -> None:
    rows = [[f"bucket-{index:03d}", index, index + 1] for index in range(251)]

    verification = verify_analysis_result(
        TableDataRead(
            columns=["bucket", "first_total", "second_total"],
            rows=rows,
            row_count=len(rows),
        ),
        [
            _assertion(
                checks=[{"kind": "unique", "required": True, "fields": ["bucket"]}],
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
                ],
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
                checks=[],
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "city_total",
                        "value_field": "total",
                        "dimension_fields": ["city"],
                    }
                ],
            )
        ],
    )
    oversized = verify_analysis_result(
        TableDataRead(columns=["city", "total"], rows=[["杭州", 10], ["上海", 20]], row_count=2),
        [
            _assertion(
                checks=[],
                claim_extractions=[
                    {
                        "mode": "series",
                        "name": "city_total",
                        "value_field": "total",
                        "dimension_fields": ["city"],
                        "max_items": 1,
                    }
                ],
            )
        ],
    )

    assert duplicate.valid is False
    assert duplicate.findings[0].code == "RESULT_SERIES_DIMENSIONS_DUPLICATE:city_total"
    assert oversized.valid is False
    assert oversized.findings[0].code == "RESULT_SERIES_MAX_ITEMS_EXCEEDED:city_total"


def test_result_verifier_checks_column_sum_equals() -> None:
    assertion = _assertion(
        checks=[
            {
                "kind": "column_sum_equals",
                "required": True,
                "value_field": "city_total",
                "total_field": "all_total",
            }
        ],
        claim_extractions=[
            {
                "mode": "series",
                "name": "all_total",
                "value_field": "all_total",
                "dimension_fields": ["city"],
            }
        ],
    )

    passed = verify_analysis_result(
        TableDataRead(
            columns=["city", "city_total", "all_total"],
            rows=[["杭州", 10, 30], ["上海", 20, 30]],
            row_count=2,
        ),
        [assertion],
    )
    failed = verify_analysis_result(
        TableDataRead(
            columns=["city", "city_total", "all_total"],
            rows=[["杭州", 10, 31], ["上海", 20, 31]],
            row_count=2,
        ),
        [assertion],
    )

    assert passed.valid is True
    assert failed.findings[0].code == "RESULT_CHECK_COLUMN_SUM_EQUALS_FAILED"
