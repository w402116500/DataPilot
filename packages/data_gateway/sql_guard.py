from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Literal

from contracts.datasources import SchemaSummaryRead
from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from data_gateway.types import SqlGuardIssue, SqlGuardLocation, SqlGuardResult

_DANGEROUS_FUNCTION_NAMES = {
    "load_file",
    "sleep",
    "benchmark",
    "get_lock",
    "release_lock",
    "release_all_locks",
    "is_free_lock",
    "is_used_lock",
    "master_pos_wait",
    "httpfs",
    "load_extension",
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_ndjson_auto",
    "read_text",
    "read_blob",
    "parquet_scan",
    "sqlite_scan",
}
_RETRYABLE_CODES = frozenset(
    {
        "SQL_EMPTY",
        "SQL_PARSE_ERROR",
        "INVALID_LIMIT",
        "MULTIPLE_STATEMENTS",
        "UNKNOWN_TABLE",
        "UNKNOWN_COLUMN",
        "COLUMN_SOURCE_MISSING",
    }
)
_MAX_SUBJECT_LENGTH = 160
_SQL_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s'\"]+[\\/])+[^\s'\"]+")
_SubjectKind = Literal["sql_fragment", "table", "column", "function", "statement", "limit"]


def guard_sql(
    sql: str,
    *,
    dialect: str,
    schema: SchemaSummaryRead,
    default_limit: int,
    max_limit: int,
) -> SqlGuardResult:
    """用 SQLGlot AST 和当前固定 Schema 验证只读 SQL，并统一处理 LIMIT。"""

    if not sql.strip():
        return _blocked(
            "SQL_EMPTY",
            "SQL 不能为空",
            subject_kind="sql_fragment",
            hint="请提交一条单独的只读查询。",
        )
    try:
        statements = parse(sql, read=dialect)
    except ParseError as exc:
        return _parse_error_result(exc, schema)
    if len(statements) != 1:
        return _blocked(
            "MULTIPLE_STATEMENTS",
            "一次只能执行一条 SQL",
            subject_kind="statement",
            subject=f"检测到 {len(statements)} 条语句",
            hint="请改为一条独立的只读查询。",
        )
    statement = statements[0]
    if (
        not isinstance(statement, exp.Query)
        or statement.find(exp.Into)
        or any(
            isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop))
            for node in statement.walk()
        )
    ):
        return _blocked(
            "WRITE_STATEMENT",
            "只允许只读查询",
            subject_kind="statement",
            subject=statement.key.upper() if statement is not None else "SQL",
        )

    unsafe_code = f"UNSAFE_{dialect.upper()}_OPERATION"
    if dialect == "mysql" and (
        any(
            isinstance(node, (exp.Lock, exp.Parameter, exp.SessionParameter, exp.PropertyEQ))
            for node in statement.walk()
        )
        or any(isinstance(node, exp.Dot) and node.find(exp.Func) for node in statement.walk())
        or "/*!" in sql
        or "/*M!" in sql
        or "/*+" in sql
    ):
        return _blocked(unsafe_code, "SQL 不允许变量、锁或可执行注释", subject_kind="statement")
    for table in statement.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            return _blocked(
                unsafe_code,
                "SQL 不允许通过表函数读取外部数据",
                subject_kind="function",
                subject=_function_subject(table.this),
            )
    for function in statement.find_all(exp.Func):
        if _is_external_function(function) or (
            dialect == "mysql"
            and isinstance(function, exp.Anonymous)
            and function.name.casefold()
            not in {
                "date_format",
                "timestampdiff",
                "timestampadd",
                "group_concat",
                "json_extract",
                "json_unquote",
                "ifnull",
                "datediff",
                "dayname",
                "monthname",
                "week",
                "weekday",
                "yearweek",
                "format",
            }
        ):
            return _blocked(
                unsafe_code,
                "SQL 使用了不允许的外部能力",
                subject_kind="function",
                subject=_function_subject(function),
            )

    cte_names = {cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE)}
    referenced_tables: list[str] = []
    table_columns = {
        table.name.casefold(): {column.name.casefold() for column in table.columns}
        for table in schema.tables
    }
    for table in statement.find_all(exp.Table):
        if table.catalog or table.db:
            return _blocked(
                "CROSS_DATASOURCE_REFERENCE",
                "不允许跨数据源引用",
                subject_kind="table",
                subject="跨数据源引用",
            )
        name = table.name
        if not name or name.casefold() in cte_names:
            continue
        if name.casefold() not in table_columns:
            return _blocked(
                "UNKNOWN_TABLE",
                "SQL 引用了不在当前 Schema 中的数据表",
                subject_kind="table",
                subject=f"FROM {_safe_fragment(name)}",
                hint="请只从当前 Schema 中已提供的数据表选择表名。",
            )
        if name not in referenced_tables:
            referenced_tables.append(name)

    cte_columns = {
        cte.alias_or_name.casefold(): {name.casefold() for name in cte.alias_column_names}
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    column_issue = _validate_columns(statement, table_columns, cte_columns)
    if column_issue is not None:
        return _blocked_issue(column_issue)

    limit = statement.args.get("limit")
    if limit is None:
        statement.limit(default_limit, copy=False)
    else:
        value = limit.expression
        if not isinstance(value, exp.Literal) or not value.is_int:
            return _blocked(
                "INVALID_LIMIT",
                "LIMIT 必须是整数",
                subject_kind="limit",
                subject=_safe_expression(value),
                hint=f"请改为不超过 {max_limit} 的整数。",
            )
        if int(value.this) > max_limit:
            statement.limit(max_limit, copy=False)
    return SqlGuardResult(
        allowed=True,
        normalized_sql=statement.sql(dialect=dialect, comments=False),
        reason_code=None,
        reason=None,
        statement_type=statement.key.upper(),
        referenced_tables=referenced_tables,
        issue=None,
    )


def _parse_error_result(error: ParseError, schema: SchemaSummaryRead) -> SqlGuardResult:
    """从 SQLGlot 的首个确定错误生成安全位置和受限提示。"""

    detail = error.errors[0] if error.errors else {}
    fragment = _safe_fragment(
        "".join(str(detail.get(key, "")) for key in ("start_context", "highlight", "end_context"))
    )
    line = detail.get("line")
    column = detail.get("col")
    location = (
        SqlGuardLocation(line=line, column=column)
        if isinstance(line, int) and line > 0 and isinstance(column, int) and column > 0
        else None
    )
    special_column = _special_column_in_fragment(fragment, schema)
    hint = "请检查该位置的 SQL 方言、括号、函数参数或字段引用。"
    if special_column is not None:
        hint = f"当前 Schema 中的字段 {special_column} 含特殊字符，必须逐字写为双引号字段名。"
    return _blocked(
        "SQL_PARSE_ERROR",
        "SQL 无法解析",
        subject_kind="sql_fragment",
        subject=fragment or None,
        location=location,
        hint=hint,
    )


def _validate_columns(
    statement: exp.Expression,
    table_columns: Mapping[str, set[str]],
    cte_columns: Mapping[str, set[str]],
) -> SqlGuardIssue | None:
    """按 SQLGlot 作用域校验可静态确定的物理字段，不依赖数据库异常文本。"""

    scopes = tuple(traverse_scope(statement))
    # ``Scope.columns`` includes columns referenced by nested queries.  Those
    # columns are owned by the nested Scope and must not be validated again
    # against the wrapper SELECT (which commonly has no FROM source, e.g. a
    # scalar subquery inside a UNION arm).
    scope_column_ids = {
        id(column): scope for scope in scopes for column in scope.expression.find_all(exp.Column)
    }
    for scope in scopes:
        source_columns = _scope_source_columns(scope, table_columns, cte_columns)
        select_aliases = {name.casefold() for name in scope.expression.named_selects}
        for column in scope.columns:
            if scope_column_ids.get(id(column)) is not scope:
                continue
            if column.is_star:
                continue
            name = column.name.casefold()
            qualifier = column.table.casefold()
            if not qualifier and _is_select_alias_reference(column, select_aliases):
                continue
            if not scope.sources:
                missing_source_issue = _known_column_missing_source_issue(column, table_columns)
                if missing_source_issue is not None:
                    return missing_source_issue
            if qualifier:
                available = source_columns.get(qualifier)
                if available is not None and name in available:
                    continue
                special_name = f"{column.table}.{column.name}"
                if _is_unquoted_special_column(column, special_name, source_columns.values()):
                    return _unknown_column_issue(special_name, needs_quotes=True)
                return _unknown_column_issue(column.sql(dialect=""), needs_quotes=False)
            if any(name in available for available in source_columns.values()):
                continue
            return _unknown_column_issue(column.sql(dialect=""), needs_quotes=False)
    return None


def _scope_source_columns(
    scope: Scope,
    table_columns: Mapping[str, set[str]],
    cte_columns: Mapping[str, set[str]],
    *,
    include_parent: bool = True,
) -> dict[str, set[str]]:
    """解析当前 SELECT 作用域的表、别名、CTE 和子查询可见字段。"""

    result: dict[str, set[str]] = {}
    for name, source in scope.sources.items():
        normalized_name = name.casefold()
        if isinstance(source, exp.Table):
            result[normalized_name] = set(table_columns.get(source.name.casefold(), set()))
        elif isinstance(source, Scope):
            result[normalized_name] = _scope_output_columns(
                source, table_columns, cte_columns
            ).union(cte_columns.get(normalized_name, set()))
    if include_parent and scope.parent is not None:
        for name, columns in _scope_source_columns(
            scope.parent,
            table_columns,
            cte_columns,
            include_parent=True,
        ).items():
            # A nested relation can shadow an outer alias.  Keep the local
            # source in that case, otherwise expose it for correlated
            # subqueries (for example ``WHERE inner.id = outer.id``).
            result.setdefault(name, columns)
    return result


def _scope_output_columns(
    scope: Scope,
    table_columns: Mapping[str, set[str]],
    cte_columns: Mapping[str, set[str]],
) -> set[str]:
    named = {column.casefold() for column in scope.expression.named_selects if column != "*"}
    source_columns = _scope_source_columns(
        scope,
        table_columns,
        cte_columns,
        include_parent=False,
    )
    for expression in scope.expression.expressions:
        if isinstance(expression, exp.Star):
            named.update(column for columns in source_columns.values() for column in columns)
        elif isinstance(expression, exp.Column) and expression.is_star:
            named.update(source_columns.get(expression.table.casefold(), set()))
    # CTE 显式列名同时定义递归体和外层引用的可见列；不能只依赖第一项 SELECT 的别名。
    return named.union(column.casefold() for column in scope.outer_columns)


def _is_select_alias_reference(column: exp.Column, select_aliases: set[str]) -> bool:
    """DuckDB/SQLite 的 ORDER BY 与 GROUP BY 可合法引用当前 SELECT 别名。"""

    return (
        isinstance(column.parent, (exp.Ordered, exp.Group))
        and column.name.casefold() in select_aliases
    )


def _is_unquoted_special_column(
    column: exp.Column,
    candidate: str,
    sources: Iterable[set[str]],
) -> bool:
    if not candidate or not isinstance(column.this, exp.Identifier) or column.this.quoted:
        return False
    normalized = candidate.casefold()
    return any(normalized in available for available in sources)


def _unknown_column_issue(subject: str, *, needs_quotes: bool) -> SqlGuardIssue:
    hint = "请只使用当前 Schema 中已提供的字段。"
    if needs_quotes:
        hint = f"当前 Schema 中的字段 {subject} 含特殊字符，必须逐字写为双引号字段名。"
    return _issue(
        "UNKNOWN_COLUMN",
        "SQL 引用了不在当前 Schema 中的字段",
        subject_kind="column",
        subject=_safe_fragment(subject),
        hint=hint,
    )


def _known_column_missing_source_issue(
    column: exp.Column,
    table_columns: Mapping[str, set[str]],
) -> SqlGuardIssue | None:
    """仅为唯一归属的已知字段提示缺失 FROM，避免猜测同名字段来源。"""

    column_name = column.name
    owners = [
        table_name
        for table_name, columns in table_columns.items()
        if column_name.casefold() in columns
    ]
    if len(owners) != 1:
        return None
    table_name = owners[0]
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    return _issue(
        "COLUMN_SOURCE_MISSING",
        "SQL 中的已知字段缺少 FROM 来源",
        subject_kind="column",
        subject=_safe_fragment(column_name),
        hint=(
            f"字段 {column_name} 属于当前 Schema 中的数据表 {table_name}，"
            "但当前 SQL 没有 FROM 来源。"
            f"如需读取该字段，请写 FROM {quoted_table}；如只需字段类型或表结构，"
            "请直接使用当前 Schema，不要执行 SQL。"
        ),
    )


def _special_column_in_fragment(fragment: str, schema: SchemaSummaryRead) -> str | None:
    lower_fragment = fragment.casefold()
    for table in schema.tables:
        for column in table.columns:
            if not _needs_identifier_quotes(column.name):
                continue
            quoted_name = f'"{column.name.casefold()}"'
            if column.name.casefold() in lower_fragment and quoted_name not in lower_fragment:
                return column.name
    return None


def _needs_identifier_quotes(name: str) -> bool:
    return not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)


def _blocked(
    code: str,
    message: str,
    *,
    subject_kind: _SubjectKind,
    subject: str | None = None,
    location: SqlGuardLocation | None = None,
    hint: str | None = None,
) -> SqlGuardResult:
    """构造统一拒绝结果，使调用方更新同一条 SQL Audit 并得到安全提示。"""

    return _blocked_issue(
        _issue(
            code,
            message,
            subject_kind=subject_kind,
            subject=subject,
            location=location,
            hint=hint,
        )
    )


def _blocked_issue(issue: SqlGuardIssue) -> SqlGuardResult:
    return SqlGuardResult(
        allowed=False,
        normalized_sql=None,
        reason_code=issue.reason_code,
        reason=issue.display_message(),
        statement_type=None,
        referenced_tables=[],
        issue=issue,
    )


def _issue(
    code: str,
    message: str,
    *,
    subject_kind: _SubjectKind,
    subject: str | None = None,
    location: SqlGuardLocation | None = None,
    hint: str | None = None,
) -> SqlGuardIssue:
    return SqlGuardIssue(
        reason_code=code,
        subject_kind=subject_kind,
        subject=_safe_fragment(subject) if subject else None,
        location=location,
        message=message,
        hint=hint,
        retryable=code in _RETRYABLE_CODES,
    )


def _safe_expression(expression: exp.Expression | None) -> str | None:
    return None if expression is None else _safe_fragment(expression.sql(dialect=""))


def _safe_fragment(value: str) -> str:
    """限制问题片段长度，并屏蔽字符串字面量与绝对路径。"""

    normalized = _SQL_STRING_LITERAL.sub("?", value)
    normalized = _ABSOLUTE_PATH.sub("[path]", normalized)
    normalized = " ".join(normalized.split())
    return normalized[:_MAX_SUBJECT_LENGTH]


def _function_subject(function: exp.Func) -> str:
    name = function.name or function.sql_name()
    return _safe_fragment(f"{name}(...)")


def _is_external_function(function: exp.Func) -> bool:
    """禁止外部读取函数；匿名函数需读取 name，不能只看 sql_name。"""

    name = (function.name or function.sql_name()).casefold()
    return name in _DANGEROUS_FUNCTION_NAMES or name.startswith("read_") or name.endswith("_scan")
