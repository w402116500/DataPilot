from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecuteErrorSpec:
    """Stable execute-time failure presented to Audit and the Agent Port."""

    code: str
    message: str
    hint: str | None
    retryable: bool


RESULT_SIZE_HINT = "请缩小查询结果规模：减少列、分组或 LIMIT。"

QUERY_IDENTIFIER_INVALID = ExecuteErrorSpec(
    code="QUERY_IDENTIFIER_INVALID",
    message="查询引用了无效的字段或标识符",
    hint="请只使用当前 Schema 中已存在的物理列名，不要把 SELECT 别名用于 WHERE 或 HAVING。",
    retryable=True,
)

QUERY_GROUP_BY_INVALID = ExecuteErrorSpec(
    code="QUERY_GROUP_BY_INVALID",
    message="查询的 GROUP BY 或聚合不合法",
    hint="请将所有非聚合列写入 GROUP BY，或改为聚合计算；避免当前方言不支持的 ROLLUP 写法。",
    retryable=True,
)

QUERY_SYNTAX_INVALID = ExecuteErrorSpec(
    code="QUERY_SYNTAX_INVALID",
    message="查询语法不被当前数据库接受",
    hint="请改写为当前数据库方言支持的只读语法。",
    retryable=True,
)

DATASOURCE_UNAVAILABLE = ExecuteErrorSpec(
    code="DATASOURCE_CHECK_FAILED",
    message="数据源连接不可用",
    hint=None,
    retryable=False,
)

QUERY_FAILED = ExecuteErrorSpec(
    code="QUERY_FAILED",
    message="查询执行失败",
    hint="已通过安全检查但执行失败，请改写为更简单的只读聚合。",
    retryable=True,
)

MYSQL_ERRNO_SPECS: dict[int, ExecuteErrorSpec] = {
    1054: QUERY_IDENTIFIER_INVALID,
    1055: QUERY_GROUP_BY_INVALID,
    1140: QUERY_GROUP_BY_INVALID,
    3065: QUERY_GROUP_BY_INVALID,
    1064: QUERY_SYNTAX_INVALID,
    1045: DATASOURCE_UNAVAILABLE,
    2003: DATASOURCE_UNAVAILABLE,
    2006: DATASOURCE_UNAVAILABLE,
    2013: DATASOURCE_UNAVAILABLE,
}

# Server gone / lost connection after the query session is ready. Not 3024 timeout.
_QUERY_INTERRUPT_ERRNOS = frozenset({2006, 2013})

_SPECS_BY_CODE: dict[str, ExecuteErrorSpec] = {
    spec.code: spec
    for spec in (
        QUERY_IDENTIFIER_INVALID,
        QUERY_GROUP_BY_INVALID,
        QUERY_SYNTAX_INVALID,
        DATASOURCE_UNAVAILABLE,
        QUERY_FAILED,
    )
}

_GUARD_CODES = frozenset(
    {
        "SQL_EMPTY",
        "SQL_PARSE_ERROR",
        "INVALID_LIMIT",
        "MULTIPLE_STATEMENTS",
        "UNKNOWN_TABLE",
        "UNKNOWN_COLUMN",
        "COLUMN_SOURCE_MISSING",
        "DATA_GATEWAY_BLOCKED",
    }
)


def spec_for_mysql_errno(errno: object, *, query_started: bool = False) -> ExecuteErrorSpec:
    """Map a pymysql integer errno; unknown values stay a generic execute failure."""

    if query_started and isinstance(errno, int) and errno in _QUERY_INTERRUPT_ERRNOS:
        return QUERY_FAILED
    if isinstance(errno, int) and errno in MYSQL_ERRNO_SPECS:
        return MYSQL_ERRNO_SPECS[errno]
    return QUERY_FAILED


def spec_for_code(code: str) -> ExecuteErrorSpec | None:
    """Look up a whitelist spec so Gateway can fill a missing hint."""

    if code in _GUARD_CODES:
        return None
    return _SPECS_BY_CODE.get(code)


def execute_failure_code(code: str) -> str:
    """Keep execute-time Audit/Port codes from impersonating Guard static codes."""

    if code in _GUARD_CODES:
        return QUERY_FAILED.code
    return code
