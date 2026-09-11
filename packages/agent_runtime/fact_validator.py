"""对最终 Markdown 中可明确绑定的业务事实执行保守校验。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from agent_runtime.contracts import AnalysisClaimValue, AnalysisScalar


@dataclass(frozen=True)
class FinalAnswerFactValidation:
    """最终答案事实校验结果；无法安全绑定的正文保持 unchecked。"""

    status: Literal["passed", "unchecked", "mismatch"]
    checked_count: int = 0
    mismatches: tuple[str, ...] = ()


@dataclass(frozen=True)
class _MarkdownTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DECIMAL_TOKEN = re.compile(r"[-+]?\d+(?:\.\d+)?")
_FACT_KEY_FIELD = re.compile(r"^[^|]+")


def validate_final_answer_facts(
    markdown: str,
    facts: Sequence[AnalysisClaimValue],
) -> FinalAnswerFactValidation:
    """只核对能由列头、维度和事实标签确定对应关系的值。"""

    if not facts:
        return FinalAnswerFactValidation(status="unchecked")
    tables = _parse_tables(markdown)
    checked = 0
    mismatches: list[str] = []
    for fact in facts:
        matches = _table_matches(tables, fact)
        if matches:
            checked += 1
            for cell, header in matches:
                if not _cell_matches_fact(cell, header, fact):
                    mismatches.append(fact.fact_key or fact.name)
            continue
        line_match = _labelled_line_match(markdown, fact)
        if line_match is None:
            continue
        checked += 1
        cell, has_unit = line_match
        if not _scalar_matches(cell, fact.value, fact.unit, has_unit):
            mismatches.append(fact.fact_key or fact.name)
    if mismatches:
        return FinalAnswerFactValidation(
            status="mismatch",
            checked_count=checked,
            mismatches=tuple(dict.fromkeys(mismatches)),
        )
    return FinalAnswerFactValidation(
        status="passed" if checked else "unchecked",
        checked_count=checked,
    )


def _parse_tables(markdown: str) -> tuple[_MarkdownTable, ...]:
    lines = markdown.splitlines()
    tables: list[_MarkdownTable] = []
    index = 0
    while index + 1 < len(lines):
        headers = _split_row(lines[index])
        divider = _split_row(lines[index + 1])
        if not headers or not divider or not all(_is_divider(cell) for cell in divider):
            index += 1
            continue
        rows: list[tuple[str, ...]] = []
        index += 2
        while index < len(lines):
            row = _split_row(lines[index])
            if not row:
                break
            rows.append(tuple(row))
            index += 1
        tables.append(_MarkdownTable(tuple(headers), tuple(rows)))
    return tuple(tables)


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if "|" not in stripped:
        return []
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_divider(cell: str) -> bool:
    return re.fullmatch(r":?-{3,}:?", cell.strip()) is not None


def _table_matches(
    tables: Sequence[_MarkdownTable],
    fact: AnalysisClaimValue,
) -> list[tuple[str, str]]:
    labels = {_normalize(fact.name)}
    field = _fact_field(fact.fact_key)
    if field:
        labels.add(_normalize(field))
    dimensions = {key: _normalize(str(value)) for key, value in fact.dimensions.items()}
    matches: list[tuple[str, str]] = []
    for table in tables:
        header_indexes: dict[str, int] = {}
        for position, header in enumerate(table.headers):
            normalized_header = _normalize(header)
            header_indexes[normalized_header] = position
            base_header = re.split(r"[\(\[]", normalized_header, maxsplit=1)[0].strip()
            if base_header:
                header_indexes.setdefault(base_header, position)
        value_index = next(
            (header_indexes[label] for label in labels if label in header_indexes),
            None,
        )
        if value_index is None:
            continue
        # 无维度事实是总量，不能绑到「渠道 | 订单金额合计」这类分组表的每一行。
        has_grouping_column = any(
            position != value_index for position, _header in enumerate(table.headers)
        )
        if not dimensions and has_grouping_column:
            continue
        dimension_indexes = {key: header_indexes.get(_normalize(key)) for key in dimensions}
        if any(index is None for index in dimension_indexes.values()):
            continue
        for row in table.rows:
            if len(row) <= value_index:
                continue
            if any(
                len(row) <= index or _normalize(row[index]) != expected
                for key, index in dimension_indexes.items()
                for expected in [dimensions[key]]
            ):
                continue
            matches.append((row[value_index], table.headers[value_index]))
    return matches


def _labelled_line_match(markdown: str, fact: AnalysisClaimValue) -> tuple[str, bool] | None:
    labels = [fact.name]
    field = _fact_field(fact.fact_key)
    if field and field not in labels:
        labels.append(field)
    for line in markdown.splitlines():
        if not any(_normalize(label) in _normalize(line) for label in labels):
            continue
        if any(
            _normalize(str(value)) not in _normalize(line) for value in fact.dimensions.values()
        ):
            continue
        label_match = next(
            (re.search(re.escape(label), line, flags=re.IGNORECASE) for label in labels if label),
            None,
        )
        if label_match is None:
            continue
        suffix = line[label_match.end() :]
        date = _ISO_DATE.search(suffix)
        if date is not None:
            return date.group(0), False
        number = _NUMBER.search(suffix)
        if number is not None:
            return number.group(0), "%" in suffix[: number.end()]
    return None


def _cell_matches_fact(cell: str, header: str, fact: AnalysisClaimValue) -> bool:
    has_unit = "%" in cell or "%" in header
    return _scalar_matches(cell, fact.value, fact.unit, has_unit)


def _scalar_matches(
    text: str,
    expected: AnalysisScalar,
    unit: str | None,
    has_unit: bool,
) -> bool:
    if _is_iso_date_value(expected):
        extracted = _extract_iso_date(text)
        return extracted is not None and extracted == _extract_iso_date(str(expected))
    actual = _extract_decimal(text)
    expected_decimal = _expected_decimal(expected)
    if actual is not None and expected_decimal is not None:
        if unit == "%" and not has_unit:
            return False
        if unit is None and has_unit:
            return False
        return _decimals_match_displayed(expected_decimal, actual)
    normalized = _normalize(text).strip("`\"'")
    expected_text = _normalize(str(expected)).strip("`\"'")
    return normalized == expected_text


def _is_iso_date_value(expected: AnalysisScalar) -> bool:
    return isinstance(expected, str) and _ISO_DATE.fullmatch(expected.strip()) is not None


def _extract_iso_date(text: str) -> str | None:
    match = _ISO_DATE.search(text)
    return match.group(0) if match else None


def _expected_decimal(expected: AnalysisScalar) -> Decimal | None:
    if isinstance(expected, bool) or expected is None:
        return None
    if isinstance(expected, int):
        return Decimal(expected)
    if isinstance(expected, float):
        return Decimal(str(expected))
    if isinstance(expected, str):
        return _parse_decimal(expected)
    return None


def _extract_decimal(text: str) -> Decimal | None:
    match = _NUMBER.search(text)
    if match is None:
        return None
    return _parse_decimal(match.group(0))


def _parse_decimal(token: str) -> Decimal | None:
    raw = token.replace(",", "").rstrip("%").strip()
    if _DECIMAL_TOKEN.fullmatch(raw) is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _decimals_match_displayed(expected: Decimal, actual: Decimal) -> bool:
    exponent = actual.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    quantum = Decimal("1").scaleb(-places)
    rounded_expected = expected.quantize(quantum, rounding=ROUND_HALF_UP)
    rounded_actual = actual.quantize(quantum, rounding=ROUND_HALF_UP)
    return rounded_expected == rounded_actual


def _fact_field(fact_key: str) -> str:
    match = _FACT_KEY_FIELD.match(fact_key)
    return match.group(0) if match else ""


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value.strip("`\"'")).lower()
