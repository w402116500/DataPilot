from __future__ import annotations

import csv
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from threading import Event, Lock, Thread, Timer

import duckdb
from contracts.datasources import (
    ForeignKeyRead,
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
    TableDataRead,
)

from data_gateway.exceptions import DataSourceCheckError, QueryCanceledError, QueryTimeoutError
from data_gateway.types import QueryCancelToken, SourceHandle

_MAX_DECIMAL_PRECISION = 38


class CsvAdapter:
    """以 Python CSV 解析器检查文件，再把受控数据写入内存 DuckDB 查询。"""

    def __init__(self, source: SourceHandle) -> None:
        self.source = source
        self._snapshot_connection: duckdb.DuckDBPyConnection | None = None
        self._snapshot_lock = Lock()

    def inspect_schema(self) -> SchemaSummaryRead:
        """完整扫描 CSV，按全部非空值推断列类型，而不是只抽样前几行。"""

        headers, stats, row_count = self._scan()
        return SchemaSummaryRead(
            datasource_id=self.source.datasource_id,
            dialect="duckdb",
            tables=[
                SchemaTableRead(
                    name="dataset",
                    columns=[
                        SchemaColumnRead(
                            name=header,
                            type=stat.schema_type,
                            nullable=stat.nullable,
                        )
                        for header, stat in zip(headers, stats, strict=True)
                    ],
                    row_count=row_count,
                )
            ],
        )

    def preview_table(self, table_name: str, limit: int) -> TableDataRead:
        """将已验证的 CSV 加载到内存 DuckDB 后读取固定单表 dataset 的前几行。"""

        if table_name != "dataset":
            raise DataSourceCheckError("UNKNOWN_TABLE", "数据表不存在")
        connection = self._open_connection()
        try:
            cursor = connection.execute('SELECT * FROM "dataset" LIMIT ?', [limit])
            return _table_result(cursor)
        finally:
            connection.close()

    def run_sql(
        self,
        sql: str,
        timeout_seconds: int,
        cancel_token: QueryCancelToken,
    ) -> TableDataRead:
        """在临时 DuckDB 执行已由 SQL Guard 审核的查询，并在超时后中断连接。"""

        if cancel_token.is_cancelled():
            raise QueryCanceledError("查询已取消")
        started_at = time.monotonic()
        deadline = started_at + max(1, timeout_seconds)
        completed = Event()
        timed_out = Event()
        connection_holder: list[duckdb.DuckDBPyConnection | None] = [None]

        def interrupt_connection() -> None:
            connection = connection_holder[0]
            if connection is not None:
                with suppress(Exception):
                    connection.interrupt()

        def interrupt_for_timeout() -> None:
            timed_out.set()
            interrupt_connection()

        def interrupt_for_cancellation() -> None:
            while not completed.wait(0.05):
                if cancel_token.is_cancelled():
                    interrupt_connection()
                    return

        timer = Timer(max(1, timeout_seconds), interrupt_for_timeout)
        cancellation_watcher = Thread(target=interrupt_for_cancellation, daemon=True)
        timer.start()
        cancellation_watcher.start()
        connection: duckdb.DuckDBPyConnection | None = None
        acquired = False
        try:
            while not acquired:
                if cancel_token.is_cancelled():
                    raise QueryCanceledError("查询已取消")
                if timed_out.is_set() or time.monotonic() >= deadline:
                    raise QueryTimeoutError("查询超时")
                acquired = self._snapshot_lock.acquire(timeout=0.05)
            connection = self._snapshot_connection
            if connection is None:
                connection = self._open_connection(
                    deadline=deadline,
                    cancel_token=cancel_token,
                    timed_out=timed_out,
                    on_connection=lambda ready: connection_holder.__setitem__(0, ready),
                )
                self._snapshot_connection = connection
            connection_holder[0] = connection
            if cancel_token.is_cancelled():
                raise QueryCanceledError("查询已取消")
            if timed_out.is_set() or time.monotonic() >= deadline:
                raise QueryTimeoutError("查询超时")
            cursor = connection.execute(sql)
            return _table_result(cursor)
        except duckdb.InterruptException as exc:
            self._discard_snapshot(connection)
            if cancel_token.is_cancelled():
                raise QueryCanceledError("查询已取消") from exc
            if timed_out.is_set() or time.monotonic() >= deadline:
                raise QueryTimeoutError("查询超时") from exc
            raise QueryTimeoutError("查询超时") from exc
        finally:
            timer.cancel()
            completed.set()
            cancellation_watcher.join(timeout=0.1)
            if connection is not None:
                if connection is not self._snapshot_connection:
                    connection.close()
            if acquired:
                self._snapshot_lock.release()

    def _discard_snapshot(self, connection: duckdb.DuckDBPyConnection | None) -> None:
        """查询被中断后丢弃连接，避免复用已被 DuckDB interrupt 的状态。"""

        if connection is None or connection is not self._snapshot_connection:
            return
        self._snapshot_connection = None
        with suppress(Exception):
            connection.close()

    def _open_connection(
        self,
        *,
        deadline: float | None = None,
        cancel_token: QueryCancelToken | None = None,
        timed_out: Event | None = None,
        on_connection: Callable[[duckdb.DuckDBPyConnection], None] | None = None,
    ) -> duckdb.DuckDBPyConnection:
        """依据保存的 Schema 建表和转换数据，保证预览与查询采用同一列类型。"""

        _raise_if_csv_query_aborted(deadline, cancel_token, timed_out)
        schema = self.source.schema
        if schema is None:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源尚未生成 Schema")
        table = _single_dataset(schema)
        connection = duckdb.connect(database=":memory:")
        try:
            if on_connection is not None:
                on_connection(connection)
            _raise_if_csv_query_aborted(deadline, cancel_token, timed_out)
            self._materialize_snapshot(
                connection,
                table.columns,
                deadline=deadline,
                cancel_token=cancel_token,
                timed_out=timed_out,
            )
            return connection
        except Exception:
            with suppress(Exception):
                connection.close()
            raise

    def _materialize_snapshot(
        self,
        connection: duckdb.DuckDBPyConnection,
        columns: list[SchemaColumnRead],
        *,
        deadline: float | None,
        cancel_token: QueryCancelToken | None,
        timed_out: Event | None,
    ) -> None:
        """用 DuckDB 原生 CSV 扫描建立固定类型快照，避免逐行 Python 插入。"""

        _raise_if_csv_query_aborted(deadline, cancel_token, timed_out)
        column_types = {column.name: _duckdb_type(column.type) for column in columns}
        raw_column_types = {column.name: "VARCHAR" for column in columns}
        projections = []
        non_empty_row_parts = []
        for column in columns:
            quoted_name = _quote_identifier(column.name)
            non_empty_row_parts.append(f"NULLIF(TRIM({quoted_name}), '') IS NOT NULL")
            if column.type == "TEXT":
                expression = f"NULLIF({quoted_name}, '')"
            else:
                expression = f"CAST(NULLIF(TRIM({quoted_name}), '') AS {column_types[column.name]})"
            projections.append(f"{expression} AS {quoted_name}")
        non_empty_row = " OR ".join(non_empty_row_parts)
        connection.execute(
            'CREATE TABLE "dataset" AS SELECT '
            f"{', '.join(projections)} FROM read_csv("
            "?, columns=?, header=true, auto_detect=false, delim=',', quote='\"', "
            "escape='\"', null_padding=true, strict_mode=false, ignore_errors=false) "
            f'AS "raw" WHERE {non_empty_row}',
            [str(self.source.source_path), raw_column_types],
        )
        _raise_if_csv_query_aborted(deadline, cancel_token, timed_out)

    def _scan(self) -> tuple[list[str], list[_ColumnStats], int]:
        """逐行校验 CSV 表头与行宽，并汇总全量列类型统计信息。"""

        headers: list[str] | None = None
        stats: list[_ColumnStats] = []
        row_count = 0
        try:
            with self.source.source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, strict=True)
                raw_headers = next(reader, None)
                headers = _validated_headers(raw_headers)
                stats = [_ColumnStats() for _ in headers]
                for raw_row in reader:
                    normalized = _normalize_csv_row(raw_row, len(headers))
                    if normalized is None:
                        continue
                    row_count += 1
                    for stat, value in zip(stats, normalized, strict=True):
                        stat.observe(value)
        except UnicodeDecodeError as exc:
            raise DataSourceCheckError(
                "CSV_ENCODING_INVALID", "CSV 不是 UTF-8 编码，请重新导出为 UTF-8 CSV"
            ) from exc
        except csv.Error as exc:
            raise DataSourceCheckError(
                "CSV_FORMAT_INVALID", "CSV 行格式错误，请检查多余单元格或未闭合引号"
            ) from exc
        except OSError as exc:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源检查失败") from exc
        if headers is None:
            raise DataSourceCheckError("CSV_HEADER_INVALID", "CSV 表头为空或有重复列名")
        return headers, stats, row_count

    def _iter_rows(self, columns: list[SchemaColumnRead]) -> Iterator[tuple[object | None, ...]]:
        """按已保存的列类型重新读取 CSV；文件被替换或结构变化时立即停止。"""

        try:
            with self.source.source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, strict=True)
                headers = _validated_headers(next(reader, None))
                if [column.name for column in columns] != headers:
                    raise DataSourceCheckError(
                        "DATASOURCE_CHECK_FAILED", "保存的 CSV Schema 与文件不一致"
                    )
                for raw_row in reader:
                    normalized = _normalize_csv_row(raw_row, len(headers))
                    if normalized is None:
                        continue
                    yield tuple(
                        _convert_csv_value(value, column.type)
                        for value, column in zip(normalized, columns, strict=True)
                    )
        except UnicodeDecodeError as exc:
            raise DataSourceCheckError(
                "CSV_ENCODING_INVALID", "CSV 不是 UTF-8 编码，请重新导出为 UTF-8 CSV"
            ) from exc
        except csv.Error as exc:
            raise DataSourceCheckError(
                "CSV_FORMAT_INVALID", "CSV 行格式错误，请检查多余单元格或未闭合引号"
            ) from exc


class SqliteAdapter:
    """只读访问受控 SQLite 文件，Schema 白名单之外的对象不会暴露给查询。"""

    def __init__(self, source: SourceHandle) -> None:
        self.source = source

    def inspect_schema(self) -> SchemaSummaryRead:
        """只读取普通用户表及其字段和外键；系统对象、View、Trigger 不进入白名单。"""

        try:
            connection = self._connect()
            try:
                table_names = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                if not table_names:
                    raise DataSourceCheckError("SQLITE_NO_USER_TABLES", "SQLite 中没有可用数据表")
                tables = [self._read_table_schema(connection, name) for name in table_names]
                return SchemaSummaryRead(
                    datasource_id=self.source.datasource_id,
                    dialect="sqlite",
                    tables=tables,
                )
            finally:
                connection.close()
        except DataSourceCheckError:
            raise
        except sqlite3.Error as exc:
            raise DataSourceCheckError("SQLITE_INVALID", "文件不是可读取的 SQLite 数据库") from exc

    def preview_table(self, table_name: str, limit: int) -> TableDataRead:
        """在 SQLite 只读连接中预览已由上层 Schema 白名单校验的表。"""

        connection = self._connect()
        try:
            connection.set_authorizer(_sqlite_authorizer)
            cursor = connection.execute(
                f"SELECT * FROM {_quote_identifier(table_name)} LIMIT ?", (limit,)
            )
            return _table_result(cursor)
        finally:
            connection.close()

    def run_sql(
        self,
        sql: str,
        timeout_seconds: int,
        cancel_token: QueryCancelToken,
    ) -> TableDataRead:
        """执行已审核的只读 SQL，并利用 SQLite 进度回调中止超时查询。"""

        if cancel_token.is_cancelled():
            raise QueryCanceledError("查询已取消")
        connection = self._connect()
        deadline = time.monotonic() + timeout_seconds

        def abort_if_timed_out() -> int:
            return int(cancel_token.is_cancelled() or time.monotonic() >= deadline)

        connection.set_authorizer(_sqlite_authorizer)
        connection.set_progress_handler(abort_if_timed_out, 1_000)
        try:
            cursor = connection.execute(sql)
            return _table_result(cursor)
        except sqlite3.OperationalError as exc:
            if cancel_token.is_cancelled():
                raise QueryCanceledError("查询已取消") from exc
            if time.monotonic() >= deadline:
                raise QueryTimeoutError("查询超时") from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """以 URI 只读模式打开受控 SQLite 文件，并禁用扩展加载。"""

        try:
            uri = f"{self.source.source_path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.enable_load_extension(False)
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise DataSourceCheckError("SQLITE_INVALID", "文件不是可读取的 SQLite 数据库") from exc

    def _read_table_schema(
        self, connection: sqlite3.Connection, table_name: str
    ) -> SchemaTableRead:
        """读取单表字段、主键、外键和行数，组成可以对外暴露的 Schema 片段。"""

        quoted = _quote_identifier(table_name)
        column_rows = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        columns = [
            SchemaColumnRead(
                name=row[1],
                type=(row[2] or "TEXT").upper(),
                nullable=not bool(row[3]),
            )
            for row in column_rows
        ]
        primary_key = [row[1] for row in column_rows if row[5]]
        groups: dict[int, list[sqlite3.Row | tuple[object, ...]]] = defaultdict(list)
        for row in connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
            groups[int(row[0])].append(row)
        foreign_keys = [
            ForeignKeyRead(
                columns=[str(row[3]) for row in rows],
                referenced_table=str(rows[0][2]),
                referenced_columns=[str(row[4]) for row in rows],
            )
            for rows in groups.values()
        ]
        row_count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        return SchemaTableRead(
            name=table_name,
            columns=columns,
            row_count=row_count,
            primary_key=primary_key,
            foreign_keys=foreign_keys,
        )


def _raise_if_csv_query_aborted(
    deadline: float | None,
    cancel_token: QueryCancelToken | None,
    timed_out: Event | None,
) -> None:
    """在 CSV 装载阶段也传播同一条查询的取消和截止时间。"""

    if cancel_token is not None and cancel_token.is_cancelled():
        raise QueryCanceledError("查询已取消")
    if (timed_out is not None and timed_out.is_set()) or (
        deadline is not None and time.monotonic() >= deadline
    ):
        raise QueryTimeoutError("查询超时")


class _ColumnStats:
    """汇总 CSV 单列的全量值，任何不安全或混合格式都会退回 TEXT。"""

    def __init__(self) -> None:
        self._kind: str | None = None
        self._force_text = False
        self._max_precision = 0
        self._max_scale = 0
        self._max_integer_digits = 0
        self.nullable = False

    def observe(self, value: str) -> None:
        """吸收一个原始单元格，并记录类型冲突、精度和空值情况。"""

        if value == "":
            self.nullable = True
            return
        kind, precision, scale = _classify_csv_value(value)
        if self._kind is None:
            self._kind = kind
        elif self._kind != kind:
            self._force_text = True
        self._max_precision = max(self._max_precision, precision)
        self._max_scale = max(self._max_scale, scale)
        self._max_integer_digits = max(self._max_integer_digits, precision - scale)
        if precision > _MAX_DECIMAL_PRECISION:
            self._force_text = True

    @property
    def schema_type(self) -> str:
        """根据已累计的统计结果给出稳定的 DuckDB 列类型。"""

        if self._kind is None or self._force_text:
            return "TEXT"
        if self._kind == "INTEGER":
            return "HUGEINT"
        if self._kind == "DECIMAL":
            # Precision and scale can peak on different rows. Reserve enough
            # integer digits for the widest value before applying the widest
            # fractional scale, otherwise DuckDB rejects valid CSV values.
            precision = max(
                self._max_precision,
                self._max_integer_digits + self._max_scale,
                self._max_scale,
                1,
            )
            if precision > _MAX_DECIMAL_PRECISION:
                return "TEXT"
            return f"DECIMAL({precision},{self._max_scale})"
        if self._kind == "DATETIME_AWARE":
            return "TIMESTAMPTZ"
        if self._kind == "DATETIME_NAIVE":
            return "TIMESTAMP"
        return self._kind


def _validated_headers(raw_headers: list[str] | None) -> list[str]:
    """校验 CSV 第一行：不能为空，去空白后不能有大小写不敏感的重复列名。"""

    if not raw_headers:
        raise DataSourceCheckError("CSV_HEADER_INVALID", "CSV 表头为空或有重复列名")
    headers = [header.strip() for header in raw_headers]
    has_duplicate_header = len({header.casefold() for header in headers}) != len(headers)
    if any(not header for header in headers) or has_duplicate_header:
        raise DataSourceCheckError("CSV_HEADER_INVALID", "CSV 表头为空或有重复列名")
    return headers


def _normalize_csv_row(row: list[str], width: int) -> list[str] | None:
    """跳过空行；短行用空值补齐，超宽行视为格式错误而不是悄悄截断。"""

    if not row or all(not value.strip() for value in row):
        return None
    if len(row) > width:
        raise DataSourceCheckError(
            "CSV_FORMAT_INVALID", "CSV 行格式错误，请检查多余单元格或未闭合引号"
        )
    return row + [""] * (width - len(row))


def _classify_csv_value(value: str) -> tuple[str, int, int]:
    """以保守规则识别 CSV 值的类型，前导零、科学计数法等格式保留为 TEXT。"""

    if _is_strict_integer(value):
        return "INTEGER", len(value.lstrip("-")), 0
    if _is_strict_decimal(value):
        digits = value.lstrip("-").replace(".", "")
        return "DECIMAL", len(digits), len(value) - value.index(".") - 1
    try:
        date.fromisoformat(value)
        return "DATE", 0, 0
    except ValueError:
        pass
    if "T" in value or " " in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ("DATETIME_AWARE" if parsed.tzinfo else "DATETIME_NAIVE"), 0, 0
        except ValueError:
            pass
    return "TEXT", 0, 0


def _is_strict_integer(value: str) -> bool:
    """只接受普通十进制整数，避免把带前导零的编号误当作数值。"""

    if value.startswith("-"):
        value = value[1:]
    return value == "0" or (value.isdigit() and not value.startswith("0"))


def _is_strict_decimal(value: str) -> bool:
    """只接受不带科学计数法和千分位的标准小数写法。"""

    if value.startswith("-"):
        value = value[1:]
    integer, separator, fraction = value.partition(".")
    return bool(separator and fraction and _is_strict_integer(integer) and fraction.isdigit())


def _convert_csv_value(value: str, schema_type: str) -> object | None:
    """按照检查时固化的类型转换单元格，避免查询阶段重新推断。"""

    if value == "":
        return None
    if schema_type == "HUGEINT":
        return int(value)
    if schema_type.startswith("DECIMAL("):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise DataSourceCheckError(
                "DATASOURCE_CHECK_FAILED", "保存的 CSV 类型无法读取"
            ) from exc
    if schema_type == "DATE":
        return date.fromisoformat(value)
    if schema_type in {"TIMESTAMP", "TIMESTAMPTZ"}:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _single_dataset(schema: SchemaSummaryRead) -> SchemaTableRead:
    """确认 CSV Schema 仍符合阶段二固定单表 dataset 的约束。"""

    if len(schema.tables) != 1 or schema.tables[0].name != "dataset":
        raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "保存的 CSV Schema 无效")
    return schema.tables[0]


def _duckdb_type(schema_type: str) -> str:
    """只允许检查阶段生成的 DuckDB 类型进入建表 SQL。"""

    allowed = {"TEXT", "HUGEINT", "DATE", "TIMESTAMP", "TIMESTAMPTZ"}
    if schema_type in allowed or schema_type.startswith("DECIMAL("):
        return schema_type
    raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "保存的 CSV 列类型无效")


def _quote_identifier(value: str) -> str:
    """转义数据库标识符，避免表名或列名被拼接成 SQL 语义。"""

    return f'"{value.replace('"', '""')}"'


def _table_result(cursor: duckdb.DuckDBPyConnection | sqlite3.Cursor) -> TableDataRead:
    """将数据库游标转换成尚未脱敏的统一表格结构。"""

    columns = [column[0] for column in cursor.description or []]
    rows = [list(row) for row in cursor.fetchall()]
    return TableDataRead(columns=columns, rows=rows, row_count=len(rows))


def _sqlite_authorizer(
    action: int,
    argument_one: str | None,
    argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    """拒绝 SQLite 的写入、DDL、事务、附加数据库和扩展加载操作。"""

    denied_actions = {
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_UPDATE,
    }
    if action in denied_actions:
        return sqlite3.SQLITE_DENY
    function_name = (argument_two or argument_one or "").lower()
    if action == sqlite3.SQLITE_FUNCTION and function_name == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
