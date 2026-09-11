"""在有限行样本上生成字段画像，并在保留前移除敏感实际取值。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from contracts.datalink import DataLinkScalar
from contracts.sensitive_fields import SensitiveFieldPolicy

from server.connector.base import MAX_SAMPLE_ROWS, DatasourceInfo, ReadOnlyConnector, SourceColumn
from server.extractor.tabular import make_graph_id
from server.models.profile import ColumnProfile

_INTEGER_TYPES = frozenset({"int", "integer", "bigint", "smallint", "tinyint"})
_FLOAT_TYPES = frozenset({"float", "real", "double", "decimal", "numeric"})
_BOOLEAN_TYPES = frozenset({"bool", "boolean"})
_DATE_TYPES = frozenset({"date"})
_DATETIME_TYPES = frozenset({"datetime", "timestamp", "timestamptz"})


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
                profiles.append(
                    self.profile_column(
                        datasource.datasource_id,
                        table.name,
                        column,
                        (row.get(column.name) for row in rows),
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
            null_rate=(total_count - len(non_null)) / total_count if total_count else 0.0,
            distinct_count=distinct_count,
            unique_rate=distinct_count / total_count if total_count else 0.0,
            top_values=top_values,
            sample_values=sample_values,
            min_value=min_value,
            max_value=max_value,
            is_sensitive=is_sensitive,
        )


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
