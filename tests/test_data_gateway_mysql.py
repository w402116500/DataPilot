from __future__ import annotations

from types import SimpleNamespace

import pytest
from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead
from contracts.sensitive_fields import SensitiveFieldPolicy
from data_gateway.exceptions import (
    DataSourceCheckError,
    QueryCanceledError,
    QueryFailedError,
    QueryTimeoutError,
)
from data_gateway.execute_errors import RESULT_SIZE_HINT, spec_for_mysql_errno
from data_gateway.mysql_adapter import (
    MAX_CELL_BYTES,
    MySqlAdapter,
    _bounded_result,
    _limit_control_command_timeout,
)
from data_gateway.service import DataGateway
from data_gateway.sql_guard import guard_sql
from data_gateway.types import GatewaySourceSnapshot, QueryCancelToken, RelationalSourceAccess
from sqlalchemy.exc import DBAPIError


def mysql_schema():
    return SchemaSummaryRead(
        datasource_id="mysql-test",
        dialect="mysql",
        tables=[
            SchemaTableRead(
                name="orders", columns=[SchemaColumnRead(name="id", type="int", nullable=False)]
            )
        ],
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET id=1",
        "SELECT 1; SELECT 2",
        "SELECT * INTO OUTFILE '/tmp/result' FROM orders",
        "SELECT * INTO DUMPFILE '/tmp/result' FROM orders",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT @x := 1",
        "SELECT @@version",
        "SELECT GET_LOCK('x', 1)",
        "SELECT SLEEP(5)",
        "SELECT BENCHMARK(100, SHA1('x'))",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT * FROM orders LOCK IN SHARE MODE",
        "SELECT * FROM other.orders",
        "SELECT * FROM information_schema.tables",
        "SELECT custom_write_function()",
        "SELECT /*!50000 SLEEP(1) */ 1",
        "SELECT /*+ MAX_EXECUTION_TIME(0) */ 1",
    ],
)
def test_mysql_guard_rejects_unsafe_operations(sql):
    result = guard_sql(sql, dialect="mysql", schema=mysql_schema(), default_limit=20, max_limit=50)
    assert not result.allowed


def test_mysql_guard_quotes_and_clamps_limit():
    result = guard_sql(
        "SELECT `id` FROM `orders` LIMIT 1000",
        dialect="mysql",
        schema=mysql_schema(),
        default_limit=20,
        max_limit=50,
    )
    assert result.allowed
    assert result.normalized_sql == "SELECT `id` FROM `orders` LIMIT 50"


def test_gateway_snapshot_does_not_share_mutable_schema_or_masks():
    source = SimpleNamespace(
        id="s",
        type="mysql",
        status="schema_ready",
        schema_revision=1,
        connection_revision=2,
        schema_cache_json=mysql_schema().model_dump(),
        mask_fields_json=["id"],
        mask_fields_confirmed=True,
        content_hash=None,
    )
    snapshot = GatewaySourceSnapshot.from_source(source)
    source.schema_cache_json["tables"].clear()
    source.mask_fields_json.clear()
    snapshot.schema_cache_json["tables"].clear()
    assert len(snapshot.schema_cache_json["tables"]) == 1
    assert snapshot.mask_fields_json == ("id",)


def test_mysql_adapter_invalid_access_is_not_sql_retryable() -> None:
    with pytest.raises(DataSourceCheckError) as caught:
        MySqlAdapter(SimpleNamespace(access=None))
    assert caught.value.code == "DATASOURCE_CHECK_FAILED"
    assert caught.value.retryable is False
    assert caught.value.code not in _GUARD_CODES


def test_mysql_access_repr_never_contains_credentials():
    access = RelationalSourceAccess("host", 3306, "db", "user", "sensitive-password")
    assert "sensitive-password" not in repr(access)
    token = QueryCancelToken()
    token.cancel()
    adapter = MySqlAdapter(SimpleNamespace(access=access))
    with pytest.raises(QueryCanceledError), adapter._connection(1, token):
        pytest.fail("Canceled operations must not connect")


def test_mysql_result_budget_stops_stream_before_remaining_rows():
    class Cursor:
        calls = 0

        def keys(self):
            return ["value"]

        def fetchone(self):
            self.calls += 1
            return ["x" * (MAX_CELL_BYTES + 1)]

    cursor = Cursor()
    with pytest.raises(DataSourceCheckError) as caught:
        _bounded_result(cursor)
    assert cursor.calls == 1
    assert caught.value.code == "QUERY_FAILED"
    assert caught.value.message == "查询结果单元格超过大小上限"
    assert caught.value.retryable is True
    assert caught.value.hint == RESULT_SIZE_HINT


_GUARD_CODES = {
    "SQL_EMPTY",
    "SQL_PARSE_ERROR",
    "INVALID_LIMIT",
    "MULTIPLE_STATEMENTS",
    "UNKNOWN_TABLE",
    "UNKNOWN_COLUMN",
    "COLUMN_SOURCE_MISSING",
    "DATA_GATEWAY_BLOCKED",
}


class _DriverError(Exception):
    def __init__(self, errno: int, message: str) -> None:
        super().__init__(errno, message)
        self.args = (errno, message)


class _FakeMysqlConnection:
    def exec_driver_sql(self, sql, *args, **kwargs):
        return None

    def close(self) -> None:
        return None

    def invalidate(self) -> None:
        return None

    @property
    def connection(self):
        return SimpleNamespace(driver_connection=SimpleNamespace(thread_id=lambda: 1))


class _FakeMysqlEngine:
    def connect(self):
        return _FakeMysqlConnection()

    def dispose(self) -> None:
        return None


class _MemoryAudit:
    def __init__(self, audit_id: str = "audit_exec_1") -> None:
        self.id = audit_id
        self.updates: list[dict[str, object]] = []


class _MemoryAudits:
    def __init__(self, audit: _MemoryAudit | None = None) -> None:
        self.audit = audit or _MemoryAudit()

    async def create_proposed(self, **_kwargs: object) -> _MemoryAudit:
        return self.audit

    async def update(self, model: _MemoryAudit, **changes: object) -> _MemoryAudit:
        model.updates.append(changes)
        for key, value in changes.items():
            setattr(model, key, value)
        return model


class _NoArtifacts:
    async def create_table_artifact(self, **_kwargs: object) -> str:
        raise AssertionError("failed queries must not persist artifacts")


class _RaisingAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def run_sql(self, sql: str, timeout_seconds: int, cancel_token: QueryCancelToken):
        raise self.error


class _FakeRegistry:
    def __init__(self, adapter: _RaisingAdapter) -> None:
        self.adapter = adapter

    def create(self, _handle: object) -> _RaisingAdapter:
        return self.adapter


def _dbapi_error(errno: int, message: str) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, _DriverError(errno, message), hide_parameters=True)


def _mysql_adapter() -> MySqlAdapter:
    access = RelationalSourceAccess("host", 3306, "db", "user", "sensitive-password")
    return MySqlAdapter(SimpleNamespace(access=access))


def _patch_mysql_engines(monkeypatch: pytest.MonkeyPatch, adapter: MySqlAdapter) -> None:
    monkeypatch.setattr(adapter, "_engine", lambda timeout, **_kwargs: _FakeMysqlEngine())


@pytest.mark.parametrize(
    ("errno", "code", "retryable"),
    [
        (1054, "QUERY_IDENTIFIER_INVALID", True),
        (1055, "QUERY_GROUP_BY_INVALID", True),
        (1140, "QUERY_GROUP_BY_INVALID", True),
        (3065, "QUERY_GROUP_BY_INVALID", True),
        (1064, "QUERY_SYNTAX_INVALID", True),
        (1045, "DATASOURCE_CHECK_FAILED", False),
        (2003, "DATASOURCE_CHECK_FAILED", False),
        (2006, "QUERY_FAILED", True),
        (2013, "QUERY_FAILED", True),
        (1234, "QUERY_FAILED", True),
    ],
)
def test_mysql_adapter_maps_execute_errno_without_driver_text(
    monkeypatch: pytest.MonkeyPatch,
    errno: int,
    code: str,
    retryable: bool,
) -> None:
    driver_text = f"Unknown column 'secret_col' at host=db.internal sql=SELECT {errno}"
    adapter = _mysql_adapter()
    _patch_mysql_engines(monkeypatch, adapter)

    with pytest.raises(DataSourceCheckError) as caught:
        with adapter._connection(5, QueryCancelToken()):
            raise _dbapi_error(errno, driver_text)

    failure = caught.value
    spec = spec_for_mysql_errno(errno, query_started=True)
    assert failure.code == code == spec.code
    assert failure.retryable is retryable is spec.retryable
    assert failure.__cause__ is None
    assert failure.code not in _GUARD_CODES
    assert driver_text not in failure.message
    assert driver_text not in (failure.hint or "")
    assert "db.internal" not in failure.message
    assert "secret_col" not in failure.message
    if failure.hint:
        assert "db.internal" not in failure.hint
        assert "secret_col" not in failure.hint


@pytest.mark.parametrize("errno", [2006, 2013])
def test_mysql_adapter_lost_connection_before_query_is_not_sql_retryable(
    monkeypatch: pytest.MonkeyPatch,
    errno: int,
) -> None:
    driver_text = f"Lost connection to MySQL server at 'db.internal' errno={errno}"
    adapter = _mysql_adapter()

    class _FailingEngine:
        def connect(self):
            raise _dbapi_error(errno, driver_text)

        def dispose(self) -> None:
            return None

    monkeypatch.setattr(adapter, "_engine", lambda timeout, **_kwargs: _FailingEngine())

    with pytest.raises(DataSourceCheckError) as caught:
        with adapter._connection(5, QueryCancelToken()):
            pytest.fail("connect-time failure must not enter the query body")

    failure = caught.value
    spec = spec_for_mysql_errno(errno, query_started=False)
    assert failure.code == "DATASOURCE_CHECK_FAILED" == spec.code
    assert failure.retryable is False is spec.retryable
    assert failure.__cause__ is None
    assert driver_text not in failure.message
    assert "db.internal" not in failure.message


def test_mysql_control_engine_keeps_access_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def fake_create_engine(_url, connect_args=None, **_kwargs):
        captured.append(dict(connect_args or {}))
        return _FakeMysqlEngine()

    monkeypatch.setattr("data_gateway.mysql_adapter.create_engine", fake_create_engine)
    adapter = _mysql_adapter()

    with adapter._connection(60, QueryCancelToken()):
        pass

    assert len(captured) == 2
    query_args, control_args = captured
    assert query_args["connect_timeout"] == 10
    assert query_args["read_timeout"] == 60
    assert query_args["write_timeout"] == 60
    assert control_args["connect_timeout"] == 10
    assert control_args["read_timeout"] == 10
    assert control_args["write_timeout"] == 10


def test_mysql_control_kill_timeout_applies_after_handshake() -> None:
    sock = SimpleNamespace(timeout=None)

    def settimeout(value):
        sock.timeout = value

    sock.settimeout = settimeout
    driver = SimpleNamespace(_read_timeout=10, _write_timeout=10, _sock=sock)
    connection = SimpleNamespace(connection=SimpleNamespace(driver_connection=driver))
    _limit_control_command_timeout(connection, 2)
    assert driver._read_timeout == 2
    assert driver._write_timeout == 2
    assert sock.timeout == 2


def test_mysql_errno_interrupt_depends_on_query_started() -> None:
    for errno in (2006, 2013):
        before = spec_for_mysql_errno(errno, query_started=False)
        after = spec_for_mysql_errno(errno, query_started=True)
        assert before.code == "DATASOURCE_CHECK_FAILED"
        assert before.retryable is False
        assert after.code == "QUERY_FAILED"
        assert after.retryable is True
        assert after.hint is not None


def test_mysql_adapter_keeps_timeout_errno_on_existing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _mysql_adapter()
    _patch_mysql_engines(monkeypatch, adapter)

    with pytest.raises(QueryTimeoutError) as caught:
        with adapter._connection(5, QueryCancelToken()):
            raise _dbapi_error(3024, "Query execution was interrupted")

    assert caught.value.__cause__ is None
    assert "max_statement_time" not in str(caught.value)


def _ready_mysql_source() -> GatewaySourceSnapshot:
    return GatewaySourceSnapshot.from_source(
        SimpleNamespace(
            id="mysql-test",
            type="mysql",
            status="schema_ready",
            schema_revision=1,
            connection_revision=0,
            schema_cache_json=mysql_schema().model_dump(),
            mask_fields_json=[],
            mask_fields_confirmed=True,
            content_hash=None,
        )
    )


def _gateway_for(error: Exception, audits: _MemoryAudits | None = None) -> DataGateway:
    return DataGateway(
        registry=_FakeRegistry(_RaisingAdapter(error)),
        audits=audits or _MemoryAudits(),
        artifacts=_NoArtifacts(),
        policy=SensitiveFieldPolicy(confirmed=True),
        default_query_limit=20,
        max_query_limit=50,
        query_timeout_seconds=30,
    )


@pytest.mark.asyncio
async def test_gateway_query_failed_error_keeps_mapped_code_and_audit_id() -> None:
    audits = _MemoryAudits()
    gateway = _gateway_for(
        DataSourceCheckError(
            "QUERY_GROUP_BY_INVALID",
            "查询的 GROUP BY 或聚合不合法",
            hint="请将所有非聚合列写入 GROUP BY，或改为聚合计算。",
            retryable=True,
        ),
        audits=audits,
    )
    access = RelationalSourceAccess("localhost", 3306, "shop", "user", "secret")

    with pytest.raises(QueryFailedError) as caught:
        await gateway.run_sql_readonly(_ready_mysql_source(), access, "SELECT id FROM orders")

    failure = caught.value
    assert failure.audit_log_id == audits.audit.id
    assert failure.code == "QUERY_GROUP_BY_INVALID"
    assert failure.retryable is True
    assert failure.__cause__ is None
    failed_update = next(item for item in audits.audit.updates if item.get("status") == "failed")
    assert failed_update["error_code"] == "QUERY_GROUP_BY_INVALID"
    assert failed_update["error_message"] == "查询的 GROUP BY 或聚合不合法"


@pytest.mark.asyncio
async def test_gateway_unclassified_exception_is_not_sql_retryable() -> None:
    audits = _MemoryAudits()
    driver_text = "pymysql (1045, Access denied for user 'root'@'db.internal')"
    gateway = _gateway_for(RuntimeError(driver_text), audits=audits)
    access = RelationalSourceAccess("localhost", 3306, "shop", "user", "secret")

    with pytest.raises(QueryFailedError) as caught:
        await gateway.run_sql_readonly(_ready_mysql_source(), access, "SELECT id FROM orders")

    failure = caught.value
    assert failure.audit_log_id == audits.audit.id
    assert failure.code == "QUERY_FAILED"
    assert failure.retryable is False
    assert failure.hint is None
    assert failure.__cause__ is None
    assert driver_text not in failure.message
    failed_update = next(item for item in audits.audit.updates if item.get("status") == "failed")
    assert failed_update["error_code"] == "QUERY_FAILED"
    assert failed_update["error_message"] == "查询执行失败"


@pytest.mark.asyncio
async def test_gateway_connection_failure_is_not_sql_retryable() -> None:
    audits = _MemoryAudits()
    gateway = _gateway_for(
        DataSourceCheckError(
            "DATASOURCE_CHECK_FAILED",
            "数据源连接不可用",
            retryable=False,
        ),
        audits=audits,
    )
    access = RelationalSourceAccess("localhost", 3306, "shop", "user", "secret")

    with pytest.raises(QueryFailedError) as caught:
        await gateway.run_sql_readonly(_ready_mysql_source(), access, "SELECT id FROM orders")

    failure = caught.value
    assert failure.audit_log_id == audits.audit.id
    assert failure.code == "DATASOURCE_CHECK_FAILED"
    assert failure.retryable is False
    assert failure.hint is None
    assert failure.__cause__ is None
    failed_update = next(item for item in audits.audit.updates if item.get("status") == "failed")
    assert failed_update["error_code"] == "DATASOURCE_CHECK_FAILED"


@pytest.mark.asyncio
async def test_gateway_execute_failure_does_not_impersonate_guard_code() -> None:
    audits = _MemoryAudits()
    gateway = _gateway_for(
        DataSourceCheckError("UNKNOWN_COLUMN", "查询引用了无效的字段或标识符", retryable=True),
        audits=audits,
    )
    access = RelationalSourceAccess("localhost", 3306, "shop", "user", "secret")

    with pytest.raises(QueryFailedError) as caught:
        await gateway.run_sql_readonly(_ready_mysql_source(), access, "SELECT id FROM orders")

    failure = caught.value
    assert failure.code == "QUERY_FAILED"
    assert failure.code not in _GUARD_CODES
    assert failure.hint is None
    failed_update = next(item for item in audits.audit.updates if item.get("status") == "failed")
    assert failed_update["error_code"] == "QUERY_FAILED"
