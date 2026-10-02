"""在有限行样本上生成字段画像，并在保留前移除敏感实际取值。"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace

from contracts.datalink import DataLinkScalar
from contracts.sensitive_fields import SensitiveFieldPolicy

from server.connector.base import (
    MAX_DISTINCT_VALUES,
    MAX_SAMPLE_ROWS,
    ConnectorError,
    DatasourceInfo,
    ReadOnlyConnector,
    SourceColumn,
)
from server.extractor.tabular import make_graph_id
from server.models.profile import ColumnProfile

logger = logging.getLogger(__name__)

_INTEGER_TYPES = frozenset({"int", "integer", "bigint", "smallint", "tinyint"})
_FLOAT_TYPES = frozenset({"float", "real", "double", "decimal", "numeric"})
_BOOLEAN_TYPES = frozenset({"bool", "boolean"})
_DATE_TYPES = frozenset({"date"})
_DATETIME_TYPES = frozenset({"datetime", "timestamp", "timestamptz"})

_MAX_TYPICAL_VALUES = 20
_MAX_TYPICAL_VALUE_CHARS = 200

_MAX_CATEGORY_DISTINCT = 50
_ID_NAME_PATTERN = re.compile(r"_id$|^id$|_no$|^no$|_code$|^code$|_number$|^number$|编号$|序号$")
_MONEY_NAME_PATTERN = re.compile(
    r"amount|price|fee|cost|revenue|gmv|金额|价格|费用|单价|总额|收入",
    re.IGNORECASE,
)


class ColumnProfiler:
    """在 Connector 的一千行上限内生成可展示、可检索的脱敏字段画像。"""

    def __init__(self, sensitive_policy: SensitiveFieldPolicy | None = None) -> None:
        self.sensitive_policy = sensitive_policy or SensitiveFieldPolicy()

    def profile_datasource(
        self, connector: ReadOnlyConnector, datasource: DatasourceInfo
    ) -> tuple[ColumnProfile, ...]:
        """按 Connector 已确认的表和列顺序读取有限样本并生成画像。"""

        profiles: list[ColumnProfile] = []
        for table in datasource.tables:
            rows = connector.sample_rows(table.name, MAX_SAMPLE_ROWS)
            for column in table.columns:
                profile = self.profile_column(
                    datasource.datasource_id,
                    table.name,
                    column,
                    (row.get(column.name) for row in rows),
                )
                profiles.append(
                    self._confirm_typical_values(
                        connector, datasource.datasource_id, table.name, column, profile
                    )
                )
        return tuple(profiles)

    def profile_column(
        self,
        datasource_id: str,
        table_name: str,
        column: SourceColumn,
        values: Iterable[DataLinkScalar | None],
    ) -> ColumnProfile:
        """为单个字段计算有限统计，敏感字段绝不保留其真实值或取值范围。"""

        observed = tuple(values)
        dtype = _canonical_dtype(column.dtype, observed)
        non_null = tuple(
            normalized
            for value in observed
            if value is not None
            for normalized in (_normalize_value(value, dtype),)
        )
        total_count = len(observed)
        distinct_count = len({_comparison_token(value) for value in non_null})
        is_sensitive = self.sensitive_policy.is_sensitive_values(column.name, observed)
        column_id = make_graph_id("column", datasource_id, table_name, column.name)

        top_values = _top_values(non_null)
        sample_values = non_null[:5]
        min_value, max_value = _numeric_range(non_null, dtype)
        typical_values: tuple[str, ...] = ()
        if not is_sensitive and 0 < distinct_count <= _MAX_TYPICAL_VALUES:
            typical_values = _typical_values(non_null)
        if is_sensitive:
            top_values = ()
            sample_values = ()
            min_value = None
            max_value = None

        return ColumnProfile(
            id=make_graph_id("profile", column_id),
            column_id=column_id,
            column_name=column.name,
            dtype=dtype,
            semantic_type=_infer_semantic_type(
                column, dtype, non_null, distinct_count, len(non_null)
            ),
            null_rate=(total_count - len(non_null)) / total_count if total_count else 0.0,
            distinct_count=distinct_count,
            unique_rate=distinct_count / total_count if total_count else 0.0,
            top_values=top_values,
            sample_values=sample_values,
            typical_values=typical_values,
            min_value=min_value,
            max_value=max_value,
            is_sensitive=is_sensitive,
        )

    def _confirm_typical_values(
        self,
        connector: ReadOnlyConnector,
        datasource_id: str,
        table_name: str,
        column: SourceColumn,
        profile: ColumnProfile,
    ) -> ColumnProfile:
        """对样本初筛命中的低基数列做全表确认，敏感列不发起查询。

        全表归并后 distinct 仍 ≤ 20 才采纳全表取值；超限说明不是低基数列，
        置空保持与现状一致。确认失败只损失增强信息，记录 warning 后回退空值，
        不让 Build 失败。
        """

        if profile.is_sensitive or not 0 < profile.distinct_count <= _MAX_TYPICAL_VALUES:
            return profile
        try:
            full_values = connector.distinct_values(table_name, column.name, MAX_DISTINCT_VALUES)
        except ConnectorError as exc:
            logger.warning(
                "DataLink typical values confirmation failed; keeping empty values",
                extra={
                    "datasource_id": datasource_id,
                    "table_name": table_name,
                    "column_name": column.name,
                    "error_code": str(exc.code),
                },
            )
            return replace(profile, typical_values=())
        merged: list[str] = []
        seen_tokens: set[str] = set()
        for value in full_values:
            token = _comparison_token(value)
            if token not in seen_tokens:
                seen_tokens.add(token)
                merged.append(str(value)[:_MAX_TYPICAL_VALUE_CHARS])
        if not 0 < len(merged) <= _MAX_TYPICAL_VALUES:
            return replace(profile, typical_values=())
        return replace(profile, typical_values=tuple(merged))


def _infer_semantic_type(
    column: SourceColumn,
    dtype: str,
    non_null: tuple[DataLinkScalar, ...],
    distinct_count: int,
    non_null_count: int,
) -> str | None:
    """按 dtype、列名与取值形态做保守的语义分类；无法确信时返回 None。

    分类供展示、同义合并的类型匹配和受限检索使用；规则必须逐字节可重复，
    不确信的列保持 None，不允许用弱证据拼凑分类。
    """

    if dtype == "boolean":
        return "boolean"
    if dtype == "datetime":
        return "timestamp"
    if dtype == "date":
        return "date"
    if column.is_primary_key or _ID_NAME_PATTERN.search(column.name):
        return "identifier"
    if dtype == "float":
        if _MONEY_NAME_PATTERN.search(column.name):
            return "monetary_value"
        return "quantity"
    if dtype == "integer":
        if non_null_count > 1 and distinct_count / non_null_count >= 0.999:
            return "identifier"
        return "quantity"
    if dtype == "text":
        if 0 < distinct_count <= _MAX_CATEGORY_DISTINCT and distinct_count < non_null_count:
            return "category"
    return None


def _canonical_dtype(declared_dtype: str, values: tuple[DataLinkScalar | None, ...]) -> str:
    """将 CSV 与 SQLite 的声明类型收敛为 Join 推断可比较的有限集合。"""

    declared = declared_dtype.casefold().strip()
    if declared in _BOOLEAN_TYPES:
        return "boolean"
    if declared in _INTEGER_TYPES:
        return "boolean" if _is_boolean_domain(values) else "integer"
    if declared in _FLOAT_TYPES:
        return "boolean" if _is_boolean_domain(values) else "float"
    if declared in _DATE_TYPES:
        return "date"
    if declared in _DATETIME_TYPES:
        return "datetime"
    return "text"


def _is_boolean_domain(values: tuple[DataLinkScalar | None, ...]) -> bool:
    """识别以 0/1 存储的布尔列，避免它们制造没有意义的 Joinable 边。"""

    non_null = {str(value).casefold() for value in values if value is not None}
    return bool(non_null) and non_null <= {"0", "1", "0.0", "1.0", "true", "false"}


def _normalize_value(value: DataLinkScalar, dtype: str) -> DataLinkScalar:
    """按已确认类型规整样本，保证数值和字符串标识符可做一致的重合比较。"""

    if dtype == "boolean":
        if isinstance(value, str):
            return value.casefold() in {"1", "true"}
        return bool(value)
    if dtype == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)
    if dtype == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _comparison_token(value: DataLinkScalar) -> str:
    """保留类型信息生成稳定的去重键，避免布尔值与整数意外相等。"""

    return f"{type(value).__name__}:{value}"


def _top_values(values: tuple[DataLinkScalar, ...], limit: int = 10) -> tuple[DataLinkScalar, ...]:
    """按频次和稳定次序保留有限常见值，不把整列值带入图谱。"""

    counts = Counter(_comparison_token(value) for value in values)
    values_by_token = {_comparison_token(value): value for value in values}
    ordered_tokens = sorted(counts, key=lambda token: (-counts[token], token))[:limit]
    return tuple(values_by_token[token] for token in ordered_tokens)


def _typical_values(values: tuple[DataLinkScalar, ...]) -> tuple[str, ...]:
    """低基数列在样本内的全部去重取值，频次和稳定次序与 Top 值一致。"""

    counts = Counter(_comparison_token(value) for value in values)
    values_by_token = {_comparison_token(value): value for value in values}
    ordered_tokens = sorted(counts, key=lambda token: (-counts[token], token))
    return tuple(str(values_by_token[token])[:_MAX_TYPICAL_VALUE_CHARS] for token in ordered_tokens)


def _numeric_range(
    values: tuple[DataLinkScalar, ...], dtype: str
) -> tuple[DataLinkScalar | None, DataLinkScalar | None]:
    """只为数值列暴露必要范围，其他类型不生成虚假的排序统计。"""

    if dtype not in {"integer", "float"} or not values:
        return None, None
    numeric_values = tuple(value for value in values if isinstance(value, (int, float)))
    if not numeric_values:
        return None, None
    return min(numeric_values), max(numeric_values)
