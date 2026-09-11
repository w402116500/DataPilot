"""对已脱敏 SQL 结果执行确定性分析检查。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from contracts.datasources import TableDataRead

from agent_runtime.contracts import (
    ANALYSIS_VERIFIED_VALUE_LIMIT,
    AnalysisAssertion,
    AnalysisScalar,
    AnalysisValidationFinding,
    AnalysisValueOperand,
    AnalysisVerifiedValue,
    analysis_fact_key,
)

_UNDEFINED: Final = object()
type _ResolvedValue = AnalysisScalar | object


@dataclass(frozen=True)
class AnalysisResultVerification:
    """一次查询结果的检查结论和可提交数值。"""

    valid: bool
    findings: list[AnalysisValidationFinding]
    verified_values: list[AnalysisVerifiedValue]


def verify_analysis_result(
    result: TableDataRead,
    assertions: Sequence[AnalysisAssertion],
    *,
    expected_columns: Sequence[str] = (),
) -> AnalysisResultVerification:
    """按 DataFoundry 的固定 resultChecks 校验已脱敏表格结果。"""

    rows = _normalize_rows(result)
    findings = _expected_column_findings(result.columns, expected_columns)
    for assertion in assertions:
        for check in assertion.result_checks:
            findings.extend(_verify_check(check, assertion.id, rows, result.row_count))

    if _has_error(findings):
        return AnalysisResultVerification(valid=False, findings=findings, verified_values=[])

    verified_values = _extract_claim_values(assertions, rows, findings)
    if len(verified_values) > ANALYSIS_VERIFIED_VALUE_LIMIT:
        findings.append(
            AnalysisValidationFinding(
                code="RESULT_VERIFIED_VALUE_LIMIT_EXCEEDED",
                message=(
                    f"查询结果展开后的可验证事实超过单次查询上限 {ANALYSIS_VERIFIED_VALUE_LIMIT}。"
                ),
                severity="error",
            )
        )
    valid = not _has_error(findings)
    return AnalysisResultVerification(
        valid=valid,
        findings=findings,
        verified_values=verified_values if valid else [],
    )


def _expected_column_findings(
    columns: Sequence[str],
    expected_columns: Sequence[str],
) -> list[AnalysisValidationFinding]:
    return [
        AnalysisValidationFinding(
            code=f"RESULT_EXPECTED_COLUMN_MISSING:{column}",
            message=f"查询结果缺少声明的结果列 {column}。",
            severity="error",
        )
        for column in expected_columns
        if column not in columns
    ]


def _verify_check(
    check: object,
    assertion_id: str,
    rows: Sequence[dict[str, AnalysisScalar]],
    row_count: int,
) -> list[AnalysisValidationFinding]:
    kind = getattr(check, "kind", None)
    required = bool(getattr(check, "required", False))
    severity = "error" if required else "warning"
    if kind == "non_empty":
        return [] if rows else [_finding("RESULT_CHECK_NON_EMPTY_FAILED", assertion_id, severity)]
    if kind == "row_count":
        minimum = check.min
        maximum = check.max
        passed = (minimum is None or row_count >= minimum) and (
            maximum is None or row_count <= maximum
        )
        return [] if passed else [_finding("RESULT_CHECK_ROW_COUNT_FAILED", assertion_id, severity)]
    if kind == "not_null":
        return [
            _finding(f"RESULT_CHECK_NOT_NULL_FAILED:{field}", assertion_id, severity)
            for field in check.fields
            if any(field not in row or row[field] is None for row in rows)
        ]
    if kind == "unique":
        keys = [json.dumps([row.get(field) for field in check.fields]) for row in rows]
        code = f"RESULT_CHECK_UNIQUE_FAILED:{','.join(check.fields)}"
        return [] if len(set(keys)) == len(keys) else [_finding(code, assertion_id, severity)]
    if kind == "equals":
        passed = _values_equal(
            _resolve_operand(check.left, rows),
            _resolve_operand(check.right, rows),
            check.tolerance or 0,
        )
        return [] if passed else [_finding("RESULT_CHECK_EQUALS_FAILED", assertion_id, severity)]
    if kind == "sum":
        total = _resolve_operand(check.total, rows)
        parts = [_resolve_operand(part, rows) for part in check.parts]
        part_sum = sum(parts) if all(_is_number(part) for part in parts) else _UNDEFINED
        passed = (
            _is_number(total)
            and _is_number(part_sum)
            and abs(total - part_sum) <= (check.tolerance or 0)
        )
        return [] if passed else [_finding("RESULT_CHECK_SUM_FAILED", assertion_id, severity)]
    if kind == "comparison":
        passed = _compare_values(
            _resolve_operand(check.left, rows),
            _resolve_operand(check.right, rows),
            check.operator,
            check.tolerance or 0,
        )
        return (
            []
            if passed
            else [
                _finding(f"RESULT_CHECK_COMPARISON_FAILED:{check.operator}", assertion_id, severity)
            ]
        )
    if kind == "budget_conservation":
        passed = _values_equal(
            _resolve_operand(check.left, rows),
            _resolve_operand(check.right, rows),
            check.tolerance or 0,
        )
        return (
            []
            if passed
            else [_finding("RESULT_CHECK_BUDGET_CONSERVATION_FAILED", assertion_id, severity)]
        )
    if kind == "ratio":
        value = _resolve_operand(check.value, rows)
        numerator = _resolve_operand(check.numerator, rows)
        denominator = _resolve_operand(check.denominator, rows)
        passed = (
            _is_number(value)
            and _is_number(numerator)
            and _is_number(denominator)
            and denominator != 0
            and abs(value - numerator / denominator * (check.scale or 1))
            <= (check.tolerance if check.tolerance is not None else 0.000001)
        )
        return [] if passed else [_finding("RESULT_CHECK_RATIO_FAILED", assertion_id, severity)]
    if kind == "column_sum_equals":
        values = [row.get(check.value_field, _UNDEFINED) for row in rows]
        totals = [row.get(check.total_field, _UNDEFINED) for row in rows]
        distinct_totals = {value for value in totals if value is not _UNDEFINED}
        passed = (
            bool(values)
            and all(_is_number(value) for value in values)
            and len(distinct_totals) == 1
            and all(_is_number(value) for value in distinct_totals)
            and abs(sum(values) - next(iter(distinct_totals))) <= check.tolerance
        )
        return (
            []
            if passed
            else [_finding("RESULT_CHECK_COLUMN_SUM_EQUALS_FAILED", assertion_id, severity)]
        )
    return []


def _extract_claim_values(
    assertions: Sequence[AnalysisAssertion],
    rows: Sequence[dict[str, AnalysisScalar]],
    findings: list[AnalysisValidationFinding],
) -> list[AnalysisVerifiedValue]:
    values: list[AnalysisVerifiedValue] = []
    for assertion in assertions:
        for extraction in assertion.claim_extractions:
            if extraction.mode == "scalar":
                value = _resolve_operand(
                    AnalysisValueOperand(field=extraction.field, selector=extraction.selector),
                    rows,
                )
                if value is _UNDEFINED:
                    if extraction.required:
                        findings.append(
                            _finding(
                                f"RESULT_CLAIM_VALUE_MISSING:{extraction.name}",
                                assertion.id,
                                "error",
                            )
                        )
                    continue
                values.append(
                    AnalysisVerifiedValue(
                        name=extraction.name,
                        value=value,
                        unit=extraction.unit,
                        tolerance=extraction.tolerance or 0,
                        assertion_id=assertion.id,
                        fact_key=analysis_fact_key(extraction.field, extraction.selector),
                        dimensions=dict(extraction.selector or {}),
                    )
                )
                continue
            _extract_series_values(assertion.id, extraction, rows, findings, values)
    return values


def _extract_series_values(
    assertion_id: str,
    extraction: object,
    rows: Sequence[dict[str, AnalysisScalar]],
    findings: list[AnalysisValidationFinding],
    values: list[AnalysisVerifiedValue],
) -> None:
    """将已校验查询行展开为带维度的事实，拒绝歧义或截断前的超量结果。"""

    if len(rows) > extraction.max_items:
        findings.append(
            _finding(f"RESULT_SERIES_MAX_ITEMS_EXCEEDED:{extraction.name}", assertion_id, "error")
        )
        return
    seen_dimensions: set[str] = set()
    for row in rows:
        dimensions = {field: row.get(field, _UNDEFINED) for field in extraction.dimension_fields}
        value = row.get(extraction.value_field, _UNDEFINED)
        if (
            value is _UNDEFINED
            or (extraction.required and value is None)
            or any(item is _UNDEFINED or item is None for item in dimensions.values())
        ):
            findings.append(
                _finding(f"RESULT_SERIES_VALUE_MISSING:{extraction.name}", assertion_id, "error")
            )
            return
        key = json.dumps(dimensions, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen_dimensions:
            findings.append(
                _finding(
                    f"RESULT_SERIES_DIMENSIONS_DUPLICATE:{extraction.name}",
                    assertion_id,
                    "error",
                )
            )
            return
        seen_dimensions.add(key)
        typed_dimensions = {
            name: value for name, value in dimensions.items() if value is not _UNDEFINED
        }
        values.append(
            AnalysisVerifiedValue(
                name=extraction.name,
                value=value,
                unit=extraction.unit,
                tolerance=extraction.tolerance,
                assertion_id=assertion_id,
                fact_key=analysis_fact_key(extraction.value_field, typed_dimensions),
                dimensions=typed_dimensions,
            )
        )


def _normalize_rows(result: TableDataRead) -> list[dict[str, AnalysisScalar]]:
    return [
        {
            column: _as_scalar(row[index]) if index < len(row) else ""
            for index, column in enumerate(result.columns)
        }
        for row in result.rows
    ]


def _resolve_operand(
    operand: AnalysisValueOperand,
    rows: Sequence[dict[str, AnalysisScalar]],
) -> _ResolvedValue:
    if "literal" in operand.model_fields_set:
        return operand.literal
    if operand.field is None:
        return _UNDEFINED
    matching_rows = (
        [
            row
            for row in rows
            if all(row.get(field) == value for field, value in operand.selector.items())
        ]
        if operand.selector is not None
        else list(rows)
    )
    if len(matching_rows) != 1:
        return _UNDEFINED
    return matching_rows[0].get(operand.field, _UNDEFINED)


def _as_scalar(value: object) -> AnalysisScalar:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _values_equal(left: _ResolvedValue, right: _ResolvedValue, tolerance: float) -> bool:
    if _is_number(left) and _is_number(right):
        return abs(left - right) <= tolerance
    return left is not _UNDEFINED and right is not _UNDEFINED and left == right


def _compare_values(
    left: _ResolvedValue,
    right: _ResolvedValue,
    operator: str,
    tolerance: float,
) -> bool:
    if operator == "eq":
        return _values_equal(left, right, tolerance)
    if not _is_number(left) or not _is_number(right):
        return False
    if operator == "gt":
        return left > right + tolerance
    if operator == "gte":
        return left >= right - tolerance
    if operator == "lt":
        return left < right - tolerance
    return left <= right + tolerance


def _finding(code: str, assertion_id: str, severity: str) -> AnalysisValidationFinding:
    return AnalysisValidationFinding(
        code=code,
        message=f"结果检查未通过：{code}。",
        severity=severity,
        assertion_id=assertion_id,
    )


def _has_error(findings: Sequence[AnalysisValidationFinding]) -> bool:
    return any(finding.severity == "error" for finding in findings)


def _is_number(value: _ResolvedValue) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
