from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from contracts.datalink import DataLinkErrorCode
from contracts.status import DataSourceType

from server.connector.base import (
    MAX_SAMPLE_ROWS,
    ConnectorError,
    DatasourceInfo,
    SourceColumn,
    SourceRow,
    SourceTable,
)

_CSV_TABLE_NAME = "dataset"
_MAX_CSV_BYTES = 100 * 1024 * 1024


class CsvConnector:
    """只读解析一个 UTF-8 CSV 文件，并将它固定表示为单表 `dataset`。"""

    def __init__(self, source_path: Path, datasource_id: str, schema_revision: int) -> None:
        self.source_path = source_path
        self.datasource_id = datasource_id
        self.schema_revision = schema_revision

    def inspect(self) -> DatasourceInfo:
        """扫描 CSV 的表头和行，生成稳定的单表 Schema。"""

        headers, stats, row_count = self._scan()
        return DatasourceInfo(
            datasource_id=self.datasource_id,
            source_type=DataSourceType.CSV,
            schema_revision=self.schema_revision,
            tables=[
                SourceTable(
                    name=_CSV_TABLE_NAME,
                    columns=[
                        SourceColumn(
                            name=header,
                            dtype=stat.dtype,
                            nullable=stat.nullable,
                            is_primary_key=False,
                        )
                        for header, stat in zip(headers, stats, strict=True)
                    ],
                    row_count=row_count,
                )
            ],
        )

    def close(self) -> None:
        """CSV opens and closes each read locally; no persistent handle remains."""

    def sample_rows(self, table_name: str, limit: int = MAX_SAMPLE_ROWS) -> list[SourceRow]:
        """返回最多一千行的 CSV 原始样本，供后续统一画像。"""

        if table_name != _CSV_TABLE_NAME:
            raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "CSV table is unavailable")
        if not 1 <= limit <= MAX_SAMPLE_ROWS:
            raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "Sample limit is invalid")

        rows: list[SourceRow] = []
        try:
            with self.source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, strict=True)
                headers = _validate_headers(next(reader, None))
                for raw_row in reader:
                    normalized = _normalize_row(raw_row, len(headers))
                    if normalized is None:
                        continue
                    rows.append(
                        {
                            header: value if value != "" else None
                            for header, value in zip(headers, normalized, strict=True)
                        }
                    )
                    if len(rows) >= limit:
                        break
        except ConnectorError:
            raise
        except UnicodeDecodeError as exc:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "CSV must use UTF-8 encoding"
            ) from exc
        except (OSError, csv.Error) as exc:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "CSV source cannot be read"
            ) from exc
        return rows

    def _scan(self) -> tuple[list[str], list[_CsvColumnStats], int]:
        """完整扫描受大小限制的 CSV，校验行宽并推断字段类型。"""

        try:
            if self.source_path.stat().st_size > _MAX_CSV_BYTES:
                raise ConnectorError(
                    DataLinkErrorCode.BUILD_FAILED, "CSV source exceeds the size limit"
                )
            with self.source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, strict=True)
                headers = _validate_headers(next(reader, None))
                stats = [_CsvColumnStats() for _ in headers]
                row_count = 0
                for raw_row in reader:
                    normalized = _normalize_row(raw_row, len(headers))
                    if normalized is None:
                        continue
                    row_count += 1
                    for stat, value in zip(stats, normalized, strict=True):
                        stat.observe(value)
        except ConnectorError:
            raise
        except UnicodeDecodeError as exc:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "CSV must use UTF-8 encoding"
            ) from exc
        except (OSError, csv.Error) as exc:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "CSV source cannot be read"
            ) from exc
        return headers, stats, row_count


class _CsvColumnStats:
    """在 CSV 扫描期间用保守规则汇总一个字段的类型。"""

    def __init__(self) -> None:
        self.nullable = False
        self._dtype: str | None = None

    @property
    def dtype(self) -> str:
        return self._dtype or "text"

    def observe(self, value: str) -> None:
        """吸收一个单元格；混合或不确定值保留为文本。"""

        if value == "":
            self.nullable = True
            return
        observed_dtype = _classify_value(value)
        if self._dtype is None:
            self._dtype = observed_dtype
        elif self._dtype == "integer" and observed_dtype == "float":
            self._dtype = "float"
        elif self._dtype != observed_dtype:
            self._dtype = "text"


def _validate_headers(raw_headers: list[str] | None) -> list[str]:
    """拒绝空、重复或仅含空白字符的 CSV 表头。"""

    if not raw_headers:
        raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "CSV header is invalid")
    headers = [header.strip() for header in raw_headers]
    if any(not header for header in headers) or len(set(headers)) != len(headers):
        raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "CSV header is invalid")
    return headers


def _normalize_row(row: list[str], width: int) -> list[str] | None:
    """跳过空行、补齐短行，并明确拒绝比表头更宽的行。"""

    if not row or all(not value.strip() for value in row):
        return None
    if len(row) > width:
        raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "CSV row has too many values")
    return row + [""] * (width - len(row))


def _classify_value(value: str) -> str:
    """以保守规则区分整数、浮点、布尔、日期时间与文本。"""

    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return "boolean"
    if _is_strict_integer(value):
        return "integer"
    if _is_strict_float(value):
        return "float"
    if "T" in value or " " in value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return "datetime"
    try:
        date.fromisoformat(value)
    except ValueError:
        return "text"
    return "date"


def _is_strict_integer(value: str) -> bool:
    """把有前导零的标识符保留为文本，避免误判为数值。"""

    unsigned = value[1:] if value.startswith(("+", "-")) else value
    if not unsigned.isdigit():
        return False
    return len(unsigned) == 1 or not unsigned.startswith("0")


def _is_strict_float(value: str) -> bool:
    """识别普通十进制小数，不把科学计数法或格式化字符串视为数值。"""

    if value.count(".") != 1 or "e" in value.casefold():
        return False
    integer, fraction = value.split(".", maxsplit=1)
    if not fraction or not _is_strict_integer(integer):
        return False
    try:
        Decimal(value)
    except InvalidOperation:
        return False
    return True
