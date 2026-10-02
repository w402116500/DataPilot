"""对已脱敏 SQL 结果做投影核对与事实提取，不预言业务真值。

验证器只做两件事：核对声明的结果列确实出现在本次执行结果的投影中
（核对对象是已发生的事实），并把结果行提取成可提交事实。业务真值——
比较、比例、行数、非空、非 NULL、唯一等“模型预言的预期”——不再在这里
裁决；值缺失是否致命由 Claim 提交时的来源核对（值必须来自已验证结果）
负责。
"""

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
from agent_runtime.query_protocol import authoritative_result_columns

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
    """核对结果投影并提取可提交事实，不执行预言式结果检查。"""

    rows = _normalize_rows(result)
    findings = _expected_column_findings(result.columns, expected_columns, assertions)
    if _has_error(findings):
        return AnalysisResultVerification(valid=False, findings=findings, verified_values=[])

    verified_values = _extract_claim_values(result.columns, assertions, rows, findings)
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
    assertions: Sequence[AnalysisAssertion] = (),
) -> list[AnalysisValidationFinding]:
    """缺少的结果列核对：finding 归因到引用该列的 assertion 供断路器统计。"""

    missing = [column for column in expected_columns if column not in columns]
    attribution: dict[str, str] = {}
    for assertion in assertions:
        declared = {_leaf_name(item) for item in authoritative_result_columns([assertion])}
        for column in missing:
            if column in attribution:
                continue
            if _leaf_name(column) in declared:
                attribution[column] = assertion.id
    if len(attribution) < len(missing) and len(assertions) == 1:
        # 查询只绑定了一个检查项且列无引用归属时，归因到该唯一目标：
        # 模型已声明这条 SQL 服务于该检查项，结果合同失败应由它承担。
        for column in missing:
            attribution.setdefault(column, assertions[0].id)
    return [
        AnalysisValidationFinding(
            code=f"RESULT_EXPECTED_COLUMN_MISSING:{column}",
            message=f"查询结果缺少声明的结果列 {column}。",
            severity="error",
            assertion_id=attribution.get(column),
        )
        for column in missing
    ]


def _leaf_name(value: str) -> str:
    stripped = value.strip().strip('"`[]').casefold()
    if stripped.count(".") == 1:
        leaf = stripped.rsplit(".", 1)[-1]
        if leaf:
            return leaf
    return stripped


def _extract_claim_values(
    projection_columns: Sequence[str],
    assertions: Sequence[AnalysisAssertion],
    rows: Sequence[dict[str, AnalysisScalar]],
    findings: list[AnalysisValidationFinding],
) -> list[AnalysisVerifiedValue]:
    values: list[AnalysisVerifiedValue] = []
    for assertion in assertions:
        for extraction in assertion.claim_extractions:
            if extraction.mode == "scalar":
                if extraction.field not in projection_columns:
                    # 投影里没有该列时提取器只如实产出“没有值”；列是否必须出现
                    # 由 expected_columns 的投影核对裁决，这里不重复判死。
                    continue
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


def _finding(code: str, assertion_id: str, severity: str) -> AnalysisValidationFinding:
    return AnalysisValidationFinding(
        code=code,
        message=f"结果检查未通过：{code}。",
        severity=severity,
        assertion_id=assertion_id,
    )


def _has_error(findings: Sequence[AnalysisValidationFinding]) -> bool:
    return any(finding.severity == "error" for finding in findings)
