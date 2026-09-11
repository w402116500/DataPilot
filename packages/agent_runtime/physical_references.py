"""冻结 Schema 上的物理表/字段引用解析。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from contracts.datasources import SchemaSummaryRead


@dataclass(frozen=True)
class PhysicalReference:
    table: str
    column: str


class PhysicalReferenceError(ValueError):
    """物理引用未知或无法唯一解析，并保留安全的机器可读诊断。"""

    def __init__(
        self,
        message: str,
        *,
        reason_code: Literal[
            "invalid_reference",
            "unknown_table",
            "unknown_column",
            "ambiguous_column",
            "table_not_allowed",
            "column_not_allowed",
        ] = "invalid_reference",
        safe_value: str | None = None,
        table: str | None = None,
        column: str | None = None,
        candidates: tuple[str, ...] = (),
    ) -> None:
        safe_message = _safe_reference_text(message)
        super().__init__(safe_message or f"physical reference validation failed: {reason_code}")
        self.reason_code = reason_code
        self.safe_value = _safe_reference_text(safe_value or "")
        self.table = _safe_reference_text(table or "") or None
        self.column = _safe_reference_text(column or "") or None
        self.candidates = tuple(_safe_reference_text(item) for item in candidates if item)[:8]


def parse_physical_reference(
    schema: SchemaSummaryRead,
    value: str,
    *,
    require_column: bool = True,
    allowed_tables: Collection[str] | None = None,
) -> PhysicalReference:
    """解析裸字段或 ``table.column``，只接受当前来源表范围内的唯一匹配。"""

    if not isinstance(value, str):
        raise PhysicalReferenceError(
            "physical reference must be a string",
            reason_code="invalid_reference",
        )
    normalized = value.strip()
    if not normalized or normalized.count(".") > 1:
        raise PhysicalReferenceError(
            f"unknown physical reference: {value}",
            reason_code="invalid_reference",
            safe_value=normalized,
        )
    parts = [part.strip('"`[] ') for part in normalized.split(".")]
    if any(not part for part in parts):
        raise PhysicalReferenceError(
            f"unknown physical reference: {value}",
            reason_code="invalid_reference",
            safe_value=normalized,
        )
    allowed = (
        {_identifier_key(table) for table in allowed_tables} if allowed_tables is not None else None
    )
    scoped_tables = [
        table
        for table in schema.tables
        if allowed is None or _identifier_key(table.name) in allowed
    ]
    if len(parts) == 2:
        table_name, column_name = parts
        tables = [
            table for table in scoped_tables if _identifier_key(table.name) == table_name.casefold()
        ]
        if not tables:
            reason_code = (
                "table_not_allowed"
                if allowed_tables is not None
                and any(
                    _identifier_key(table.name) == table_name.casefold() for table in schema.tables
                )
                else "unknown_table"
            )
            candidates = tuple(table.name for table in scoped_tables)
            raise PhysicalReferenceError(
                f"unknown physical table: {table_name}",
                reason_code=reason_code,
                safe_value=table_name,
                table=table_name,
                candidates=candidates,
            )
        columns = [
            column
            for column in tables[0].columns
            if column.name.casefold() == column_name.casefold()
        ]
        if not columns:
            raise PhysicalReferenceError(
                f"unknown physical column: {value}",
                reason_code="unknown_column",
                safe_value=column_name,
                table=tables[0].name,
                column=column_name,
                candidates=tuple(
                    f"{tables[0].name}.{column.name}" for column in tables[0].columns
                ),
            )
        return PhysicalReference(table=tables[0].name, column=columns[0].name)
    if not require_column:
        tables = [
            table for table in scoped_tables if _identifier_key(table.name) == parts[0].casefold()
        ]
        if len(tables) != 1:
            all_tables = [
                table
                for table in schema.tables
                if _identifier_key(table.name) == parts[0].casefold()
            ]
            reason_code = "table_not_allowed" if not tables and all_tables else "unknown_table"
            raise PhysicalReferenceError(
                f"unknown physical table: {value}",
                reason_code=reason_code,
                safe_value=parts[0],
                table=parts[0],
                candidates=tuple(table.name for table in scoped_tables),
            )
        return PhysicalReference(table=tables[0].name, column="")
    matches = [
        (table.name, column.name)
        for table in scoped_tables
        for column in table.columns
        if column.name.casefold() == parts[0].casefold()
    ]
    if not matches:
        if allowed_tables is not None:
            outside_matches = [
                (table.name, column.name)
                for table in schema.tables
                if table not in scoped_tables
                for column in table.columns
                if column.name.casefold() == parts[0].casefold()
            ]
            if outside_matches:
                table_name, column_name = outside_matches[0]
                raise PhysicalReferenceError(
                    f"physical column is outside allowed source tables: {parts[0]}",
                    reason_code="column_not_allowed",
                    safe_value=column_name,
                    table=table_name if len(outside_matches) == 1 else None,
                    column=column_name,
                    candidates=tuple(f"{table}.{column}" for table, column in outside_matches),
                )
        all_matches = tuple(
            f"{table.name}.{column.name}"
            for table in scoped_tables
            for column in table.columns
        )
        raise PhysicalReferenceError(
            f"unknown physical column: {value}",
            reason_code="unknown_column",
            safe_value=parts[0],
            column=parts[0],
            candidates=all_matches,
        )
    if len(matches) > 1:
        raise PhysicalReferenceError(
            f"ambiguous physical column: {value}",
            reason_code="ambiguous_column",
            safe_value=parts[0],
            column=parts[0],
            candidates=tuple(f"{table}.{column}" for table, column in matches),
        )
    return PhysicalReference(table=matches[0][0], column=matches[0][1])


def normalize_physical_reference(schema: SchemaSummaryRead, value: str) -> tuple[str, str]:
    """返回供合同校验复用的规范化 ``(table, column)``。"""

    reference = parse_physical_reference(schema, value)
    return reference.table, reference.column


resolve_physical_reference = parse_physical_reference


def _identifier_key(value: str) -> str:
    return value.strip('"`[] ').casefold()


def _safe_reference_text(value: str) -> str:
    """保留短标识诊断，避免异常对象携带 SQL/路径等原文。"""

    text = " ".join(str(value).split()).strip()
    if not text or len(text) > 160 or any(ord(char) < 32 for char in text):
        return ""
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "password",
            "passwd",
            "api_key",
            "apikey",
            "secret",
            "token",
            "authorization",
            "credential",
            "bearer ",
            "select ",
            "insert ",
            "update ",
            "delete ",
            "drop ",
            " from ",
            " where ",
        )
    ):
        return ""
    if "/" in text or "\\" in text or "://" in text or ";" in text:
        return ""
    return text
