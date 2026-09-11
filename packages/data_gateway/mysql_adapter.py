from __future__ import annotations

import logging
import ssl
import time
from contextlib import contextmanager, suppress
from threading import Event, Thread

from contracts.datasources import (
    ForeignKeyRead,
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
    TableDataRead,
)
from sqlalchemy import URL, create_engine, inspect
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from data_gateway.exceptions import DataSourceCheckError, QueryCanceledError, QueryTimeoutError
from data_gateway.execute_errors import RESULT_SIZE_HINT, spec_for_mysql_errno
from data_gateway.types import QueryCancelToken, RelationalSourceAccess, SourceHandle

logger = logging.getLogger(__name__)
# After handshake, KILL CONNECTION uses a short socket timeout so watcher.join
# cannot inherit the query read budget. Do not apply this cap to control
# connect/read/write: pymysql uses read_timeout for handshake packets, and a 2s
# engine timeout surfaces as errno 2003/2013 before the query session starts.
_CONTROL_COMMAND_TIMEOUT_SECONDS = 2

MAX_RESULT_ROWS = 5000
MAX_RESULT_COLUMNS = 512
MAX_CELL_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024


class MySqlAdapter:
    """One disposable read-only connection per operation; credentials stay private."""

    def __init__(self, source: SourceHandle) -> None:
        if not isinstance(source.access, RelationalSourceAccess):
            raise DataSourceCheckError(
                "DATASOURCE_CHECK_FAILED",
                "连接配置无效",
                retryable=False,
            )
        self.source = source
        self._access = source.access

    def _engine(
        self,
        timeout: int,
        *,
        connect_timeout: int | None = None,
        read_timeout: int | None = None,
        write_timeout: int | None = None,
    ):
        access = self._access
        query_budget = max(1, timeout)
        options = dict(
            connect_timeout=max(
                1,
                min(access.connect_timeout_seconds, query_budget)
                if connect_timeout is None
                else connect_timeout,
            ),
            read_timeout=max(1, query_budget if read_timeout is None else read_timeout),
            write_timeout=max(1, query_budget if write_timeout is None else write_timeout),
            local_infile=False,
            charset="utf8mb4",
        )
        if access.tls:
            options["ssl"] = ssl.create_default_context()
        else:
            options["ssl_disabled"] = True
        return create_engine(
            URL.create(
                "mysql+pymysql",
                username=access.username,
                password=access.password,
                host=access.host,
                port=access.port,
                database=access.database,
            ),
            connect_args=options,
            poolclass=NullPool,
            hide_parameters=True,
        )

    @contextmanager
    def _connection(self, timeout: int, token: QueryCancelToken):
        deadline = time.monotonic() + timeout
        if token.is_cancelled():
            raise QueryCanceledError("查询已取消")
        engine = self._engine(timeout)
        handshake_budget = min(self._access.connect_timeout_seconds, max(1, timeout))
        kill_budget = min(_CONTROL_COMMAND_TIMEOUT_SECONDS, max(1, timeout))
        control_engine = self._engine(
            timeout,
            connect_timeout=handshake_budget,
            read_timeout=handshake_budget,
            write_timeout=handshake_budget,
        )
        done, timed_out = Event(), Event()
        watcher = None
        connection = control = None
        thread_id = None
        succeeded = False
        query_started = False
        phase = "connect_control"
        try:
            # A separate authenticated connection can kill this account's own query.
            # Never issue protocol commands concurrently on the query connection.
            control = control_engine.connect()
            _limit_control_command_timeout(control, kill_budget)
            phase = "connect_query"
            connection = engine.connect()
            thread_id = connection.connection.driver_connection.thread_id()

            def stop_query():
                while not done.wait(0.025):
                    if token.is_cancelled() or time.monotonic() >= deadline:
                        if not token.is_cancelled():
                            timed_out.set()
                        with suppress(SQLAlchemyError):
                            control.exec_driver_sql(f"KILL CONNECTION {int(thread_id)}")
                        return

            watcher = Thread(target=stop_query, daemon=True)
            watcher.start()
            phase = "setup"
            connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {max(1, timeout * 1000)}")
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.exec_driver_sql("START TRANSACTION READ ONLY")
            if token.is_cancelled():
                raise QueryCanceledError("查询已取消")
            if time.monotonic() >= deadline:
                raise QueryTimeoutError("查询超时")
            query_started = True
            phase = "execute"
            yield connection
            if token.is_cancelled():
                raise QueryCanceledError("查询已取消")
            if timed_out.is_set() or time.monotonic() >= deadline:
                raise QueryTimeoutError("查询超时")
            succeeded = True
        except (SQLAlchemyError, OSError) as exc:
            errno = _mysql_errno(exc)
            if token.is_cancelled():
                raise QueryCanceledError("查询已取消") from None
            if timed_out.is_set() or time.monotonic() >= deadline or errno == 3024:
                logger.warning(
                    "mysql adapter timeout datasource_id=%s errno=%s phase=%s query_started=%s",
                    getattr(self.source, "datasource_id", None),
                    errno,
                    phase,
                    query_started,
                )
                raise QueryTimeoutError("查询超时") from None
            spec = spec_for_mysql_errno(errno, query_started=query_started)
            logger.warning(
                "mysql adapter execute mapped datasource_id=%s errno=%s "
                "phase=%s query_started=%s code=%s retryable=%s",
                getattr(self.source, "datasource_id", None),
                errno,
                phase,
                query_started,
                spec.code,
                spec.retryable,
            )
            raise DataSourceCheckError(
                spec.code,
                spec.message,
                hint=spec.hint,
                retryable=spec.retryable,
            ) from None
        finally:
            done.set()
            if watcher is not None:
                watcher.join()
            if not succeeded and connection is not None:
                if control is not None and thread_id is not None:
                    with suppress(SQLAlchemyError):
                        control.exec_driver_sql(f"KILL CONNECTION {int(thread_id)}")
                # Do not drain an oversized unbuffered result during rollback/close.
                with suppress(SQLAlchemyError):
                    connection.invalidate()
            # NullPool prevents interrupted transactions or old credentials being reused.
            for item in (connection, control):
                if item is not None:
                    with suppress(SQLAlchemyError):
                        item.close()
            engine.dispose()
            control_engine.dispose()

    def inspect_schema(self) -> SchemaSummaryRead:
        try:
            with self._connection(30, QueryCancelToken()) as connection:
                inspector = inspect(connection)
                names = inspector.get_table_names()
                if len(names) > 200:
                    raise DataSourceCheckError(
                        "DATASOURCE_CHECK_FAILED", "数据库表数量超过检查上限"
                    )
                tables = []
                for name in names:
                    columns = inspector.get_columns(name)
                    if len(columns) > MAX_RESULT_COLUMNS:
                        raise DataSourceCheckError(
                            "DATASOURCE_CHECK_FAILED", "数据表字段数量超过上限"
                        )
                    foreign_keys = [
                        ForeignKeyRead(
                            columns=key["constrained_columns"],
                            referenced_table=key["referred_table"],
                            referenced_columns=key["referred_columns"],
                        )
                        for key in inspector.get_foreign_keys(name)
                        if key.get("referred_schema") in (None, self._access.database)
                    ]
                    tables.append(
                        SchemaTableRead(
                            name=name,
                            columns=[
                                SchemaColumnRead(
                                    name=column["name"],
                                    type=str(column["type"]).lower(),
                                    nullable=column["nullable"],
                                )
                                for column in columns
                            ],
                            primary_key=inspector.get_pk_constraint(name).get("constrained_columns")
                            or [],
                            foreign_keys=foreign_keys,
                            row_count=None,
                        )
                    )
                return SchemaSummaryRead(
                    datasource_id=self.source.datasource_id,
                    dialect="mysql",
                    tables=tables,
                )
        except (DataSourceCheckError, QueryTimeoutError):
            raise DataSourceCheckError(
                "DATASOURCE_CHECK_FAILED",
                "数据库结构检查失败",
                retryable=False,
            ) from None

    def preview_table(self, table_name: str, limit: int) -> TableDataRead:
        if self.source.schema is None or table_name not in {
            table.name for table in self.source.schema.tables
        }:
            raise DataSourceCheckError("UNKNOWN_TABLE", "数据表不存在")
        with self._connection(30, QueryCancelToken()) as connection:
            quoted = connection.dialect.identifier_preparer.quote_identifier(table_name)
            cursor = connection.execution_options(stream_results=True).exec_driver_sql(
                f"SELECT * FROM {quoted} LIMIT %s",
                (max(1, min(limit, 50)),),
            )
            return _bounded_result(cursor)

    def run_sql(
        self, sql: str, timeout_seconds: int, cancel_token: QueryCancelToken
    ) -> TableDataRead:
        with self._connection(timeout_seconds, cancel_token) as connection:
            cursor = connection.execution_options(stream_results=True).exec_driver_sql(sql)
            return _bounded_result(cursor)


def _limit_control_command_timeout(connection, seconds: int) -> None:
    """Shorten KILL I/O only. Handshake already finished on this connection."""

    driver = getattr(getattr(connection, "connection", None), "driver_connection", None)
    if driver is None:
        return
    if hasattr(driver, "_read_timeout"):
        driver._read_timeout = seconds
    if hasattr(driver, "_write_timeout"):
        driver._write_timeout = seconds
    sock = getattr(driver, "_sock", None)
    if sock is not None:
        with suppress(OSError):
            sock.settimeout(seconds)


def _mysql_errno(exc: BaseException) -> int | None:
    """Read only pymysql's integer errno; never pass args[1] or str(exc) upward."""

    if not isinstance(exc, DBAPIError):
        return None
    orig = exc.orig
    args = getattr(orig, "args", None) if orig is not None else None
    if not args:
        return None
    code = args[0]
    return code if isinstance(code, int) else None


def _result_size_error(message: str) -> DataSourceCheckError:
    return DataSourceCheckError(
        "QUERY_FAILED",
        message,
        hint=RESULT_SIZE_HINT,
        retryable=True,
    )


def _bounded_result(cursor) -> TableDataRead:
    columns = list(cursor.keys())
    if len(columns) > MAX_RESULT_COLUMNS:
        raise _result_size_error("查询结果字段数量超过上限")
    rows, size = [], 0
    while True:
        row = cursor.fetchone()
        if row is None:
            break
        if len(rows) >= MAX_RESULT_ROWS:
            raise _result_size_error("查询结果行数超过上限")
        values = list(row)
        for value in values:
            length = len(value) if isinstance(value, bytes) else len(str(value).encode("utf-8"))
            if length > MAX_CELL_BYTES:
                raise _result_size_error("查询结果单元格超过大小上限")
            size += length
        if size > MAX_RESULT_BYTES:
            raise _result_size_error("查询结果超过大小上限")
        rows.append(values)
    cursor.close()
    return TableDataRead(columns=columns, rows=rows, row_count=len(rows))
