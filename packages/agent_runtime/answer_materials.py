"""Bounded final-answer materials. No IO and no additional model calls."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence

from contracts.answer_materials import AnswerMaterial, AnswerMaterialSource, ChartIntent, ChartSpec
from contracts.datalink import DataLinkSemanticContext
from contracts.datasources import SchemaSummaryRead

from agent_runtime.chart_joins import confirmed_join_labels
from agent_runtime.contracts import (
    AnalysisClaimValue,
    AnalysisEvidenceBinding,
    AnalysisQueryAttempt,
    AnalysisReportedClaim,
    analysis_fact_key,
)


def admitted_query_attempts(
    attempts: Sequence[AnalysisQueryAttempt],
    bindings: Sequence[AnalysisEvidenceBinding],
    claims: Sequence[AnalysisReportedClaim],
) -> list[AnalysisQueryAttempt]:
    """Project only verified values admitted by a submitted claim for this query."""
    result = []
    for attempt in attempts:
        if not attempt.valid:
            continue
        binding_ids = {item.id for item in bindings if item.query_attempt_id == attempt.id}
        admitted = [value for claim in claims
                    if binding_ids.intersection(claim.evidence_binding_ids)
                    for value in claim.values]
        verified = [value for value in attempt.verified_values if any(
            value.name == claim.name and value.fact_key == claim.fact_key
            and value.value == claim.value and value.dimensions == claim.dimensions
            for claim in admitted)]
        if verified:
            result.append(attempt.model_copy(update={"verified_values": verified}))
    return result


def available_chart_sources(attempts: Sequence[AnalysisQueryAttempt]) -> list[dict[str, object]]:
    """List admitted reference choices and columns without exposing unsubmitted rows."""
    sources = []
    for attempt in attempts:
        values = [AnalysisClaimValue.model_validate(
            value.model_dump(exclude={"assertion_id", "tolerance"})
        ) for value in attempt.verified_values]
        units = submitted_result_units(attempt, values)
        if not units:
            continue
        fields = sorted(set.intersection(*(set(unit) for unit in units)))
        numeric = [field for field in fields if all(
            isinstance(unit[field], (int, float)) and not isinstance(unit[field], bool)
            and math.isfinite(unit[field]) for unit in units
        )]
        sources.append({
            "source_refs": [ref for ref in (attempt.audit_log_id, attempt.artifact_id) if ref],
            "fields": fields, "numeric_fields": numeric, "row_count": len(units),
        })
    return sources


def submitted_result_units(
    attempt: AnalysisQueryAttempt, values: Sequence[AnalysisClaimValue]
) -> list[dict[str, object]]:
    """Keep full safe rows for submitted fields; never admit other query statistics."""
    if attempt.safe_result is None:
        return [value.model_dump(mode="json", exclude={"fact_key"}) for value in values]
    units: list[dict[str, object]] = []
    for row in attempt.safe_result.rows:
        source = dict(zip(attempt.safe_result.columns, row, strict=False))
        visible: dict[str, object] = {}
        for assertion in attempt.assertions:
            for extraction in assertion.claim_extractions:
                field = extraction.field if extraction.mode == "scalar" else extraction.value_field
                for value in values:
                    if (
                        field not in source
                        or value.name != extraction.name
                        or source[field] != value.value
                        or value.fact_key != analysis_fact_key(field, value.dimensions)
                    ):
                        continue
                    if any(source.get(key) != item for key, item in value.dimensions.items()):
                        continue
                    visible.update(value.dimensions)
                    visible[field] = source[field]
        if visible:
            units.append(visible)
    return units


def material_from_units(
    *,
    kind: str,
    title: str,
    units: Sequence[dict[str, object]],
    scope: str,
    sources: list[AnswerMaterialSource],
    max_chars: int = 12_000,
    supports: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> AnswerMaterial | None:
    """Preserve whole safe rows/relationships; explicitly disclose excluded units."""
    lines: list[str] = []
    used = 0
    for unit in units:
        line = json.dumps(unit, ensure_ascii=False, separators=(",", ":"))
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return None
    scope += f" 本材料显示 {len(lines)}/{len(units)} 个完整条目。"
    if len(lines) < len(units):
        scope += "材料已截断，未展示部分不能据此推断。"
    return AnswerMaterial(
        number=1,
        kind=kind,
        title=title[:240],
        content="\n".join(lines),
        scope=scope,
        sources=sources,
        supports=list(supports),
        limitations=list(limitations),
    )


def number_materials(materials: Sequence[AnswerMaterial]) -> tuple[AnswerMaterial, ...]:
    """Assign numbers only after collecting and stably ordering the full material set."""
    result: list[AnswerMaterial] = []
    seen: set[str] = set()
    priority = {
        "query_result": 0, "table": 0, "chart_spec": 1, "schema": 2,
        "relationship": 3, "file": 4, "chart": 4, "markdown": 4, "warning": 5,
    }
    for material in sorted(materials, key=lambda item: priority[item.kind]):
        key = material.model_dump_json(exclude={"number"})
        if key in seen:
            continue
        seen.add(key)
        result.append(material.model_copy(update={"number": len(result) + 1}))
    return tuple(result)


def build_chart_spec(
    intent: ChartIntent,
    attempt: AnalysisQueryAttempt,
    *,
    schema: SchemaSummaryRead | None = None,
    semantic_context: DataLinkSemanticContext | None = None,
    sibling_attempts: Sequence[AnalysisQueryAttempt] = (),
    dialect: str | None = None,
) -> ChartSpec:
    """Validate declared fields against admitted facts, without claiming to inspect pixels."""
    if not attempt.valid or intent.source_ref not in (attempt.audit_log_id, attempt.artifact_id):
        raise ValueError("CHART_SOURCE_INVALID")
    values = [
        AnalysisClaimValue.model_validate(value.model_dump(exclude={"assertion_id", "tolerance"}))
        for value in attempt.verified_values
    ]
    units = submitted_result_units(attempt, values)
    fields = {intent.x_field, intent.y_metric}
    if intent.group_by:
        fields.add(intent.group_by)
    if not units or any(not fields.issubset(unit) for unit in units):
        raise ValueError("CHART_FIELDS_UNVERIFIED")
    metrics = [unit[intent.y_metric] for unit in units]
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) for value in metrics
    ):
        raise ValueError("CHART_METRIC_NOT_NUMERIC")
    returned_count = attempt.safe_result.row_count if attempt.safe_result else len(units)
    dimensions = {
        field: len({json.dumps(unit[field], ensure_ascii=False) for unit in units})
        for field in fields - {intent.y_metric}
    }
    coverage_notes = _group_coverage_notes(units, fields - {intent.y_metric}, sibling_attempts)
    coverage = (
        f"查询返回 {returned_count} 行，图表可引用 {len(units)} 行已准入事实；"
        f"维度不同值数量：{json.dumps(dimensions, ensure_ascii=False)}"
    )
    if coverage_notes:
        coverage += "；分组覆盖：" + "；".join(coverage_notes)
    joins = confirmed_join_labels(attempt.sql, schema, semantic_context, dialect)
    limitations = ["仅覆盖已返回且已提交的查询事实，不代表数据库全部记录。"]
    if joins.limitation:
        limitations.append(joins.limitation)
    if coverage_notes:
        limitations.append("分组覆盖：" + "；".join(coverage_notes) + "。")
    return ChartSpec(
        intent=intent,
        source_ref=intent.source_ref,
        row_count=len(units),
        coverage=coverage[:500],
        verified_columns=sorted(fields),
        metric_min=min(metrics),
        metric_max=max(metrics),
        confirmed_joins=list(joins.labels),
        limitations=limitations,
    )


def _group_coverage_notes(
    units: Sequence[dict[str, object]],
    fields: set[str],
    sibling_attempts: Sequence[AnalysisQueryAttempt],
) -> list[str]:
    notes: list[str] = []
    for field in sorted(fields):
        chart_values = _keyed_values(unit[field] for unit in units if field in unit)
        if not chart_values:
            continue
        best: dict[str, object] | None = None
        for sibling in sibling_attempts:
            sibling_values = _sibling_column_values(sibling, field)
            if sibling_values is None:
                continue
            covered = set(chart_values).issubset(sibling_values)
            if covered and len(sibling_values) > len(chart_values):
                if best is None or len(sibling_values) > len(best):
                    best = sibling_values
        if best is None:
            continue
        missing = [best[key] for key in sorted(set(best) - set(chart_values))]
        shown = "、".join(_display_value(value) for value in missing[:12])
        note = f"{field} {len(chart_values)}/{len(best)}，未覆盖 {shown}"
        hidden = len(missing) - 12
        if hidden > 0:
            note += f"，其余 {hidden} 个"
        notes.append(note)
    return notes


def _sibling_column_values(attempt: AnalysisQueryAttempt, field: str) -> dict[str, object] | None:
    if attempt.safe_result is not None and field in attempt.safe_result.columns:
        index = attempt.safe_result.columns.index(field)
        return _keyed_values(row[index] for row in attempt.safe_result.rows if index < len(row))
    values = [
        AnalysisClaimValue.model_validate(value.model_dump(exclude={"assertion_id", "tolerance"}))
        for value in attempt.verified_values
    ]
    units = submitted_result_units(attempt, values)
    present = [unit[field] for unit in units if field in unit]
    if not present:
        return None
    return _keyed_values(present)


def _keyed_values(values: Iterable[object]) -> dict[str, object]:
    found: dict[str, object] = {}
    for value in values:
        found[json.dumps(value, ensure_ascii=False, default=str)] = value
    return found


def _display_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
