from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from contracts.datasources import TableDataRead
from contracts.sensitive_fields import SensitiveFieldPolicy

_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def serialize_table_result(result: TableDataRead, policy: SensitiveFieldPolicy) -> TableDataRead:
    """将 Adapter 原始结果变成可返回、可存储且已脱敏的普通值。"""

    format_sensitive_fields = policy.sensitive_fields_for_rows(result.columns, result.rows)
    return TableDataRead(
        columns=result.columns,
        rows=[
            [
                serialize_value(
                    value,
                    policy,
                    column_name,
                    format_sensitive=column_name in format_sensitive_fields,
                )
                for column_name, value in zip(result.columns, row, strict=True)
            ]
            for row in result.rows
        ],
        row_count=result.row_count,
    )


def serialize_value(
    value: Any,
    policy: SensitiveFieldPolicy,
    field_name: str | None = None,
    *,
    format_sensitive: bool = False,
) -> Any:
    """递归处理敏感字段、二进制、精确数值和日期时间，避免由消费者二次猜测。"""

    if field_name is not None and (format_sensitive or policy.is_sensitive_field(field_name)):
        return policy.mask_value
    if value is None or isinstance(value, (bool, float)):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _MAX_SAFE_INTEGER else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[二进制数据，{len(value)} 字节]"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): serialize_value(item, policy, str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_value(item, policy) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if isinstance(parsed, (dict, list)):
                return serialize_value(parsed, policy)
        return value
    return str(value)
