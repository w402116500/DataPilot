"""SQL 查询业务契约的执行前确定性校验。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from contracts.datasources import SchemaSummaryRead
from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from agent_runtime.contracts import AnalysisAssertion, AnalysisValidationFinding
from agent_runtime.physical_references import PhysicalReferenceError, parse_physical_reference
from agent_runtime.sql_scope import SqlScopeBuildError, SqlScopeIndex


@dataclass(frozen=True)
class SqlContractPreflight:
    """一次 SQL 业务契约预检的不可变结果。"""

    valid: bool
    findings: list[AnalysisValidationFinding]
    expected_columns: list[str]


@dataclass(frozen=True)
class SqlScopePreflight:
    """一次受限 SQL 作用域预检的不可变结果。"""

    valid: bool
    findings: list[AnalysisValidationFinding]


@dataclass(frozen=True)
class _Aggregate:
    function: str
    column: str | None
    table: str | None
    alias: str | None
    direct: bool = True
    distinct: bool = False
    expression_type: str = "aggregate"


@dataclass(frozen=True)
class _SqlColumnReference:
    """SQL AST 中已经解析出的物理字段及其真实表归属。"""

    table: str | None
    column: str
    alias: str | None = None
    expression_type: str = "column"
    physical: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _Predicate:
    column: str
    table: str | None
    operator: str
    value: object


def preflight_discovery_scope(
    sql: str,
    *,
    dialect: str | None,
    schema: SchemaSummaryRead,
    allowed_tables: Sequence[str],
    allowed_columns: Sequence[str],
) -> SqlScopePreflight:
    """使用 SQLGlot 作用域检查 Discovery SQL 的物理 allowlist。

    CTE、子查询和 SELECT 别名是当前 SQL 的派生关系，不会被当成物理表或物理字段；
    它们的底层列仍会在各自的 SQLGlot scope 中逐层校验。
    """

    findings: list[AnalysisValidationFinding] = []
    schema_columns = {
        _normalize(table.name): {_normalize(column.name) for column in table.columns}
        for table in schema.tables
    }
    schema_tables = set(schema_columns)
    allowed_table_names = {_normalize(table) for table in allowed_tables}
    allowed_columns_by_table: dict[str, set[str]] = {table: set() for table in allowed_table_names}
    try:
        for value in allowed_columns:
            reference = parse_physical_reference(
                schema,
                value,
                allowed_tables=allowed_tables,
            )
            allowed_columns_by_table.setdefault(_normalize(reference.table), set()).add(
                _normalize(reference.column)
            )
    except PhysicalReferenceError as error:
        return SqlScopePreflight(
            valid=False,
            findings=[_finding("DISCOVERY_SCOPE_INVALID", str(error))],
        )

    try:
        statements = parse(sql, read=_parser_dialect(dialect))
    except ParseError:
        return SqlScopePreflight(
            valid=False,
            findings=[_finding("DISCOVERY_SQL_PARSE_ERROR", "Discovery SQL 无法解析。")],
        )
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return SqlScopePreflight(
            valid=False,
            findings=[_finding("DISCOVERY_SQL_INVALID", "Discovery 只能执行一条只读查询。")],
        )

    statement = statements[0]
    if statement.find(exp.Insert) or statement.find(exp.Update) or statement.find(exp.Delete):
        return SqlScopePreflight(
            valid=False,
            findings=[_finding("DISCOVERY_SQL_INVALID", "Discovery 只能执行只读查询。")],
        )

    cte_names = _cte_names(statement)
    for table in statement.find_all(exp.Table):
        table_name = _normalize(table.name)
        if not table_name or table_name in cte_names:
            continue
        if table_name not in schema_tables:
            findings.append(
                _finding(
                    "DISCOVERY_SCOPE_TABLE_UNKNOWN",
                    f"Discovery SQL 引用了当前 Schema 不存在的数据表 {table.name}。",
                )
            )
        elif table_name not in allowed_table_names:
            findings.append(
                _finding(
                    "DISCOVERY_SCOPE_TABLE_FORBIDDEN",
                    f"Discovery SQL 使用了不在探索白名单中的数据表 {table.name}。",
                )
            )

    # ``SqlScopeIndex`` is the source of truth for column scope.  The old
    # flattened ``scope.sources`` pass treated CTE/subquery output aliases as
    # physical columns and rejected valid UNION/CTE discovery queries before
    # lineage resolution had a chance to prove their physical origins.
    # Keep the explicit star check because a partial allowlist must reject
    # ``table.*`` even though SQLGlot expands it during qualification.
    for scope in traverse_scope(statement):
        physical_sources = _discovery_physical_sources(scope, cte_names)
        for star in scope.expression.find_all(exp.Star):
            if isinstance(star.parent, exp.Count):
                continue
            for alias, table_name in physical_sources.items():
                allowed_columns = allowed_columns_by_table.get(_normalize(table_name), set())
                schema_table_columns = schema_columns.get(_normalize(table_name), set())
                if allowed_columns != schema_table_columns:
                    _append_unique_finding(
                        findings,
                        _finding(
                            "DISCOVERY_SCOPE_COLUMN_FORBIDDEN",
                            f"Discovery SQL 的 {alias}.* 超出了字段探索白名单。",
                        ),
                    )
                    break
    try:
        scope_index = SqlScopeIndex(statement, schema, _parser_dialect(dialect) or "")
    except SqlScopeBuildError:
        # Preserve precise legacy diagnostics for ordinary unknown/ambiguous
        # physical references when qualification itself cannot succeed.  This
        # fallback is intentionally not used for successfully qualified
        # derived relations, so CTE output names never become fake physical
        # references.
        _append_discovery_raw_scope_findings(
            findings,
            statement=statement,
            schema_columns=schema_columns,
            allowed_columns_by_table=allowed_columns_by_table,
            cte_names=cte_names,
        )
        if not any(
            finding.code
            in {
                "DISCOVERY_SCOPE_COLUMN_INVALID",
                "DISCOVERY_SCOPE_COLUMN_AMBIGUOUS",
                "DISCOVERY_SCOPE_COLUMN_FORBIDDEN",
            }
            for finding in findings
        ):
            _append_unique_finding(
                findings,
                _finding(
                    "DISCOVERY_SCOPE_COLUMN_INVALID",
                    "Discovery SQL 的字段或派生关系无法在当前 Schema 中唯一解析。",
                ),
            )
    else:
        scope_columns_cache: dict[int, dict[str, set[str]]] = {}
        scope_output_cache: dict[int, set[str]] = {}
        cte_columns = _discovery_cte_columns(statement)
        for _scope, column, lineage in scope_index.iter_columns():
            if lineage.status != "resolved":
                if _discovery_column_is_derived_output(
                    _scope,
                    column,
                    schema_columns=schema_columns,
                    cte_columns=cte_columns,
                    cte_names=cte_names,
                    scope_columns_cache=scope_columns_cache,
                    scope_output_cache=scope_output_cache,
                ):
                    continue
                _append_unique_finding(
                    findings,
                    _finding(
                        "DISCOVERY_SCOPE_COLUMN_INVALID",
                        f"Discovery SQL 引用了无法唯一解析的字段 {column.sql()}。",
                    ),
                )
                continue
            for physical in lineage.physical:
                table_name = _normalize(physical.table)
                column_name = _normalize(physical.column)
                if table_name not in allowed_table_names:
                    _append_unique_finding(
                        findings,
                        _finding(
                            "DISCOVERY_SCOPE_TABLE_FORBIDDEN",
                            f"Discovery SQL 使用了不在探索白名单中的数据表 {physical.table}。",
                        ),
                    )
                elif column_name not in allowed_columns_by_table.get(table_name, set()):
                    _append_unique_finding(
                        findings,
                        _finding(
                            "DISCOVERY_SCOPE_COLUMN_FORBIDDEN",
                            (
                                "Discovery SQL 使用了不在探索白名单中的字段 "
                                f"{physical.table}.{physical.column}。"
                            ),
                        ),
                    )

    return SqlScopePreflight(valid=not findings, findings=findings)


def preflight_sql_contract(
    sql: str,
    *,
    dialect: str | None,
    assertions: Sequence[AnalysisAssertion],
    schema: SchemaSummaryRead | None = None,
) -> SqlContractPreflight:
    """用 SQLGlot AST 比较 SQL 与当前 assertions，不访问外部数据源。"""

    expected_columns = authoritative_result_columns(assertions)
    findings: list[AnalysisValidationFinding] = []
    if schema is not None:
        for assertion in assertions:
            try:
                source_tables = tuple(assertion.source_tables)
                source_table_names = {_normalize(table) for table in source_tables}
                for table in assertion.source_tables:
                    parse_physical_reference(schema, table, require_column=False)
                for constraint in assertion.sql_constraints:
                    if constraint.kind == "source":
                        if _normalize(constraint.table) not in source_table_names:
                            findings.append(
                                _finding(
                                    f"SQL_CONTRACT_SOURCE_OUT_OF_SCOPE:{constraint.table}",
                                    (
                                        f"来源约束声明了 {constraint.table}，但该表不在当前 "
                                        "assertion 的 "
                                        "source_tables 中。"
                                    ),
                                    assertion.id,
                                )
                            )
                            continue
                        parse_physical_reference(
                            schema,
                            constraint.table,
                            require_column=False,
                            allowed_tables=source_tables,
                        )
                    elif constraint.kind == "aggregate" and constraint.column not in {None, "*"}:
                        parse_physical_reference(
                            schema,
                            constraint.column,
                            allowed_tables=source_tables or None,
                        )
                    elif constraint.kind == "group_by":
                        result_names = _assertion_result_names(assertion)
                        for column in constraint.columns:
                            if _normalize(column) in result_names and not _is_qualified_reference(
                                column
                            ):
                                continue
                            parse_physical_reference(
                                schema,
                                column,
                                allowed_tables=source_tables or None,
                            )
                    else:
                        for column in _constraint_columns(constraint):
                            if column != "*":
                                parse_physical_reference(
                                    schema,
                                    column,
                                    allowed_tables=source_tables or None,
                                )
            except PhysicalReferenceError as exc:
                findings.append(
                    _finding(
                        "SQL_CONTRACT_PHYSICAL_REFERENCE_INVALID",
                        str(exc),
                        assertion.id,
                    )
                )

    try:
        statements = parse(sql, read=_parser_dialect(dialect))
    except ParseError as error:
        # SQL 语法、只读性和物理字段错误仍由 Data Gateway SQL Guard 所有；
        # 这里不把业务预检变成第二个安全执行器。
        del error
        return _result(findings, expected_columns)
    if len(statements) != 1:
        return _result(findings, expected_columns)
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        return _result(findings, expected_columns)

    scope_index: SqlScopeIndex | None = None
    if schema is not None:
        try:
            scope_index = SqlScopeIndex(
                statement,
                schema,
                _parser_dialect(dialect) or dialect or "",
            )
        except SqlScopeBuildError as error:
            if not error.defer_to_gateway:
                findings.append(
                    _finding(
                        "SQL_CONTRACT_SCOPE_INVALID",
                        "SQL 的字段或派生关系无法在当前 Schema 中唯一解析。",
                    )
                )
                return _result(findings, expected_columns)
            # Unknown physical fields remain a Data Gateway concern so it can
            # produce the existing retryable UNKNOWN_COLUMN/quoting hint.
            return _result(findings, expected_columns)
        statement = scope_index.statement

    tables = {
        _normalize(table.name)
        for table in statement.find_all(exp.Table)
        if table.name and _normalize(table.name) not in _cte_names(statement)
    }
    aliases = {
        _normalize(expression.alias)
        for expression in statement.expressions
        if isinstance(expression, exp.Alias) and expression.alias
    }
    selected_names = {
        _normalize(expression.alias_or_name)
        for expression in statement.expressions
        if expression.alias_or_name
    }
    scope_lookup = _scope_lookup(statement)
    column_references = _collect_column_references(
        statement,
        schema=schema,
        scope_lookup=scope_lookup,
        scope_index=scope_index,
    )
    projection_references = _collect_projection_references(
        statement,
        schema=schema,
        scope_lookup=scope_lookup,
        scope_index=scope_index,
    )
    aggregates = _collect_aggregates(
        statement,
        schema=schema,
        scope_lookup=scope_lookup,
        scope_index=scope_index,
    )
    group_expressions = tuple(
        expression
        for scope in traverse_scope(statement)
        for expression in (
            scope.expression.args.get("group").expressions
            if scope.expression.args.get("group")
            else ()
        )
    )
    group_by_references = _collect_expression_references(
        group_expressions,
        schema=schema,
        scope_lookup=scope_lookup,
        scope_index=scope_index,
    )
    if any(
        any(isinstance(node, exp.AggFunc) for node in expression.walk())
        for expression in group_expressions
    ):
        findings.append(
            _finding(
                "SQL_CONTRACT_GROUP_BY_AGGREGATE",
                "GROUP BY 不能直接使用聚合表达式；请先在子查询或 CTE 中计算指标，"
                "再按结果字段分组。",
            )
        )
    projected_expressions = {
        _normalize(expression.alias): expression.this
        for scope in traverse_scope(statement)
        for expression in scope.expression.expressions
        if isinstance(expression, exp.Alias) and expression.alias
    }
    predicates = _collect_all_predicates(
        statement,
        schema=schema,
        scope_lookup=scope_lookup,
        scope_index=scope_index,
    )

    for assertion in assertions:
        for table in assertion.source_tables:
            if _normalize(table) not in tables:
                findings.append(
                    _finding(
                        f"SQL_CONTRACT_SOURCE_MISSING:{table}",
                        f"正式契约要求读取数据表 {table}，但 SQL 没有使用它。",
                        assertion.id,
                    )
                )
        for constraint in assertion.sql_constraints:
            if constraint.kind == "source" and _normalize(constraint.table) not in tables:
                findings.append(
                    _finding(
                        f"SQL_CONTRACT_SOURCE_MISSING:{constraint.table}",
                        f"正式契约要求读取数据表 {constraint.table}，但 SQL 没有使用它。",
                        assertion.id,
                    )
                )
            elif constraint.kind == "column" and not _column_reference_matches(
                column_references,
                constraint.column,
                schema=schema,
                allowed_tables=assertion.source_tables,
            ):
                findings.append(
                    _finding(
                        f"SQL_CONTRACT_COLUMN_MISSING:{constraint.column}",
                        f"正式契约要求使用字段 {constraint.column}，但 SQL 没有引用它。",
                        assertion.id,
                    )
                )
            elif constraint.kind == "aggregate":
                if not _aggregate_matches(
                    aggregates,
                    constraint,
                    schema=schema,
                    allowed_tables=assertion.source_tables,
                ):
                    expected = _format_expected_aggregate(constraint)
                    observed = (
                        ", ".join(_format_aggregate(item) for item in aggregates)
                        or "没有聚合表达式"
                    )
                    findings.append(
                        _finding(
                            f"SQL_CONTRACT_AGGREGATE_MISMATCH:{constraint.function}",
                            f"正式契约要求 {expected}，但 SQL 实际为 {observed}。",
                            assertion.id,
                        )
                    )
            elif constraint.kind == "group_by":
                for column in constraint.columns:
                    if not _group_by_matches(
                        column,
                        group_by_references,
                        group_expressions,
                        projected_expressions,
                        schema=schema,
                        scope_lookup=scope_lookup,
                        scope_index=scope_index,
                        allowed_tables=assertion.source_tables,
                    ):
                        findings.append(
                            _finding(
                                f"SQL_CONTRACT_GROUP_BY_MISSING:{column}",
                                f"正式契约要求按字段 {column} 分组，但 SQL 的 GROUP BY 中没有它。",
                                assertion.id,
                            )
                        )
            elif constraint.kind == "filter":
                if not _predicate_matches(
                    predicates,
                    constraint.column,
                    constraint.operator,
                    constraint.value,
                    schema=schema,
                    allowed_tables=assertion.source_tables,
                ):
                    findings.append(
                        _finding(
                            f"SQL_CONTRACT_FILTER_MISSING:{constraint.column}",
                            (
                                f"正式契约要求过滤 {constraint.column} {constraint.operator}，"
                                "但 SQL 没有对应条件。"
                            ),
                            assertion.id,
                        )
                    )
            elif constraint.kind == "time_range":
                if not _predicate_matches(
                    predicates,
                    constraint.column,
                    "gte",
                    constraint.start,
                    schema=schema,
                    allowed_tables=assertion.source_tables,
                ):
                    findings.append(
                        _finding(
                            f"SQL_CONTRACT_TIME_START_MISSING:{constraint.column}",
                            f"正式契约要求时间起点 {constraint.start}，但 SQL 没有对应边界。",
                            assertion.id,
                        )
                    )
                end_operator = "lt" if not constraint.end_inclusive else "lte"
                if not _predicate_matches(
                    predicates,
                    constraint.column,
                    end_operator,
                    constraint.end,
                    schema=schema,
                    allowed_tables=assertion.source_tables,
                ):
                    findings.append(
                        _finding(
                            f"SQL_CONTRACT_TIME_END_MISSING:{constraint.column}",
                            f"正式契约要求时间终点 {constraint.end}，但 SQL 没有对应边界。",
                            assertion.id,
                        )
                    )
        for result_column in authoritative_result_columns([assertion]):
            field = _column_name(result_column)
            projection_matches = (
                _column_reference_matches(
                    projection_references,
                    result_column,
                    schema=schema,
                    allowed_tables=assertion.source_tables,
                )
                if _is_qualified_reference(result_column)
                else field in aliases or field in selected_names
            )
            if not projection_matches:
                findings.append(
                    _finding(
                        f"SQL_CONTRACT_RESULT_COLUMN_MISSING:{result_column}",
                        (f"正式契约要求结果字段 {result_column}，但 SQL 投影没有提供该字段。"),
                        assertion.id,
                    )
                )

    return _result(findings, expected_columns)


def _result(
    findings: list[AnalysisValidationFinding],
    expected_columns: list[str],
) -> SqlContractPreflight:
    return SqlContractPreflight(
        valid=not findings, findings=findings, expected_columns=expected_columns
    )


def _constraint_columns(constraint: object) -> tuple[str, ...]:
    """返回 SQL 合同中需要解析的物理字段。"""

    kind = getattr(constraint, "kind", None)
    if kind in {"column", "filter", "time_range"}:
        return (constraint.column,)
    if kind == "aggregate" and getattr(constraint, "column", None):
        return (constraint.column,)
    if kind == "group_by":
        return tuple(constraint.columns)
    return ()


def authoritative_result_columns(assertions: Sequence[AnalysisAssertion]) -> list[str]:
    """按正式契约派生 Runtime、Gateway 和校验器共用的结果字段。"""

    result: list[str] = []
    seen: set[str] = set()
    for assertion in assertions:
        for column in assertion.result_columns:
            _append_column(result, seen, column)
        for extraction in assertion.claim_extractions:
            if extraction.mode == "scalar":
                _append_column(result, seen, extraction.field)
            else:
                _append_column(result, seen, extraction.value_field)
                for column in extraction.dimension_fields:
                    _append_column(result, seen, column)
        for dimension in assertion.dimensions:
            _append_column(result, seen, dimension)
        # GROUP BY constrains SQL execution; output projection is owned by
        # result_columns, dimensions, claim extractions, and result checks.
        for extraction in assertion.claim_extractions:
            if extraction.mode == "scalar":
                for selector_key in extraction.selector or {}:
                    _append_column(result, seen, selector_key)
        for check in assertion.result_checks:
            for column in _result_check_columns(check):
                _append_column(result, seen, column)
    return result


def _result_check_columns(check: object) -> Iterable[str]:
    if getattr(check, "kind", None) == "column_sum_equals":
        yield check.value_field
        yield check.total_field
        return
    yield from getattr(check, "fields", ())
    for name in ("left", "right", "total", "value", "numerator", "denominator"):
        operand = getattr(check, name, None)
        if operand is not None:
            yield from _operand_columns(operand)
    for operand in getattr(check, "parts", ()):
        yield from _operand_columns(operand)


def _operand_columns(operand: object) -> Iterable[str]:
    field = getattr(operand, "field", None)
    if field:
        yield field
    yield from getattr(operand, "selector", None) or {}


def _append_column(result: list[str], seen: set[str], column: str) -> None:
    normalized = _normalize(column)
    if normalized in seen:
        return
    seen.add(normalized)
    result.append(column)


def _collect_aggregates(
    statement: exp.Expression,
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None,
) -> list[_Aggregate]:
    result: list[_Aggregate] = []
    # Contract aggregates may be calculated inside a CTE and projected by an
    # outer SELECT. Only a direct COUNT/SUM/AVG-style argument can satisfy an
    # aggregate contract; CASE, arithmetic, nested queries, windows and
    # COUNT(DISTINCT ...) remain non-direct and must use result checks instead.
    for scope in traverse_scope(statement):
        for expression in scope.expression.expressions:
            alias = expression.alias if isinstance(expression, exp.Alias) else None
            aggregate = expression.this if isinstance(expression, exp.Alias) else expression
            if not isinstance(aggregate, exp.AggFunc):
                continue
            argument = aggregate.this
            distinct = isinstance(argument, exp.Distinct)
            direct_argument = (
                argument.expressions[0] if distinct and argument.expressions else argument
            )
            direct = isinstance(direct_argument, (exp.Column, exp.Star)) and not distinct
            if isinstance(direct_argument, exp.Star):
                column, table = "*", None
            elif isinstance(direct_argument, exp.Column):
                reference = _resolve_column_reference(
                    direct_argument,
                    schema=schema,
                    scope_lookup=scope_lookup,
                    scope_index=scope_index,
                )
                column, table = direct_argument.name, reference.table
            else:
                column, table = None, None
            result.append(
                _Aggregate(
                    function=aggregate.sql_name().casefold(),
                    column=column,
                    table=table,
                    alias=alias,
                    direct=direct
                    and (
                        scope_index is None
                        or isinstance(direct_argument, exp.Star)
                        or (
                            isinstance(direct_argument, exp.Column)
                            and scope_index.is_direct_physical_column(
                                direct_argument,
                                scope_lookup.get(id(direct_argument)),
                            )
                        )
                    ),
                    distinct=distinct,
                )
            )
    return result


def _aggregate_matches(
    aggregates: Sequence[_Aggregate],
    constraint: object,
    *,
    schema: SchemaSummaryRead | None,
    allowed_tables: Sequence[str],
) -> bool:
    expected_function = str(getattr(constraint, "function", "")).casefold()
    expected_column = getattr(constraint, "column", None)
    expected_alias = getattr(constraint, "alias", None)
    expected_identity = _physical_identity(
        expected_column,
        schema=schema,
        allowed_tables=allowed_tables,
    )
    if expected_function not in {"count", "sum", "avg"}:
        return False
    return any(
        item.direct
        and not item.distinct
        and item.function == expected_function
        and _aggregate_column_matches(
            item,
            expected_column,
            expected_identity,
            allowed_tables,
        )
        and (expected_alias is None or _normalize(item.alias or "") == _normalize(expected_alias))
        for item in aggregates
    )


def _group_by_matches(
    column: str,
    group_references: Sequence[_SqlColumnReference],
    group_expressions: Sequence[exp.Expression],
    projected_expressions: dict[str, exp.Expression],
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None,
    allowed_tables: Sequence[str],
) -> bool:
    normalized = _normalize(column)
    # GROUP BY may legally use a SELECT alias (for example order_month).
    if any(
        isinstance(expression, exp.Column) and _normalize(expression.alias_or_name) == normalized
        for expression in group_expressions
    ):
        return True
    projected = projected_expressions.get(normalized)
    if projected is not None and any(
        projected == expression or projected.sql() == expression.sql()
        for expression in group_expressions
    ):
        return True
    # Some dialects allow GROUP BY to reference a SELECT alias. Resolve that
    # alias back to its AST expression before matching the physical field.
    for expression in group_expressions:
        if not isinstance(expression, exp.Column):
            continue
        projected = projected_expressions.get(_normalize(expression.alias_or_name))
        if projected is None:
            continue
        projected_references = _collect_expression_references(
            (projected,),
            schema=schema,
            scope_lookup=scope_lookup,
            scope_index=scope_index,
        )
        if _column_reference_matches(
            projected_references,
            column,
            schema=schema,
            allowed_tables=allowed_tables,
        ):
            return True
    # A physical time field can be grouped through a derived bucket such as
    # strftime('%Y-%m', signup_date) while the bucket alias owns the result.
    return _column_reference_matches(
        group_references,
        column,
        schema=schema,
        allowed_tables=allowed_tables,
    )


def _collect_predicates(
    where: exp.Expression | None,
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None,
) -> list[_Predicate]:
    if where is None:
        return []
    condition = where.this if isinstance(where, exp.Where) else where
    if isinstance(condition, exp.And):
        return [
            *_collect_predicates(
                condition.left,
                schema=schema,
                scope_lookup=scope_lookup,
                scope_index=scope_index,
            ),
            *_collect_predicates(
                condition.right,
                schema=schema,
                scope_lookup=scope_lookup,
                scope_index=scope_index,
            ),
        ]
    # SQLGlot represents ``column IS NULL`` as Is(column, Null()) and
    # ``column IS NOT NULL`` as Not(Is(column, Null())).  Keep these as
    # explicit predicate operators instead of treating them as ``= NULL``;
    # SQL three-valued logic makes the latter incorrect and most engines
    # never match it.
    null_operator: str | None = None
    if isinstance(condition, exp.Is) and isinstance(condition.expression, exp.Null):
        null_operator = "is_null"
        left = condition.this
        right = None
    elif (
        isinstance(condition, exp.Not)
        and isinstance(condition.this, exp.Is)
        and isinstance(condition.this.expression, exp.Null)
    ):
        null_operator = "is_not_null"
        left = condition.this.this
        right = None
    elif isinstance(condition, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left = condition.left
        right = condition.right
    else:
        return []
    if not isinstance(left, exp.Column):
        return []
    if null_operator is not None:
        operator = null_operator
        value = None
    else:
        value = _literal_value(right)
        if value is _MISSING:
            return []
        operator = _operator_name(condition)
    reference = _resolve_column_reference(
        left,
        schema=schema,
        scope_lookup=scope_lookup,
        scope_index=scope_index,
    )
    return [_Predicate(left.name, reference.table, operator, value)]


def _collect_all_predicates(
    statement: exp.Expression,
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None,
) -> list[_Predicate]:
    result: list[_Predicate] = []
    for scope in traverse_scope(statement):
        result.extend(
            _collect_predicates(
                scope.expression.args.get("where"),
                schema=schema,
                scope_lookup=scope_lookup,
                scope_index=scope_index,
            )
        )
    return result


_MISSING = object()


def _literal_value(value: exp.Expression) -> object:
    if isinstance(value, exp.Literal):
        if value.is_string:
            return value.this
        if value.is_int:
            return int(value.this)
        try:
            return float(value.this)
        except ValueError:
            return value.this
    if isinstance(value, exp.Null):
        return None
    return _MISSING


def _operator_name(condition: exp.Expression) -> str:
    return {
        exp.EQ: "eq",
        exp.GT: "gt",
        exp.GTE: "gte",
        exp.LT: "lt",
        exp.LTE: "lte",
    }[type(condition)]


def _predicate_matches(
    predicates: Iterable[_Predicate],
    column: str,
    operator: str,
    value: object,
    *,
    schema: SchemaSummaryRead | None,
    allowed_tables: Sequence[str],
) -> bool:
    expected_identity = _physical_identity(
        column,
        schema=schema,
        allowed_tables=allowed_tables,
    )
    return any(
        _column_identity_matches(
            item.table,
            item.column,
            column,
            expected_identity,
            allowed_tables,
        )
        and item.operator == operator
        and item.value == value
        for item in predicates
    )


def _cte_names(statement: exp.Expression) -> set[str]:
    return {
        _normalize(cte.alias_or_name) for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }


def _discovery_cte_columns(statement: exp.Expression) -> dict[str, set[str]]:
    return {
        _normalize(cte.alias_or_name): {_normalize(name) for name in cte.alias_column_names}
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }


def _discovery_scope_source_columns(
    scope: Scope,
    *,
    schema_columns: Mapping[str, set[str]],
    cte_columns: Mapping[str, set[str]],
    scope_columns_cache: dict[int, dict[str, set[str]]],
    scope_output_cache: dict[int, set[str]],
) -> dict[str, set[str]]:
    cached = scope_columns_cache.get(id(scope))
    if cached is not None:
        return cached
    result: dict[str, set[str]] = {}
    scope_columns_cache[id(scope)] = result
    for alias, source in _discovery_selected_sources(scope):
        normalized_alias = _normalize(alias)
        if isinstance(source, exp.Table):
            source_name = _normalize(source.name)
            result[normalized_alias] = set(
                cte_columns.get(source_name, schema_columns.get(source_name, set()))
            )
        elif isinstance(source, Scope):
            result[normalized_alias] = set(
                _discovery_scope_output_columns(
                    source,
                    schema_columns=schema_columns,
                    cte_columns=cte_columns,
                    scope_columns_cache=scope_columns_cache,
                    scope_output_cache=scope_output_cache,
                )
            )
    return result


def _discovery_scope_output_columns(
    scope: Scope,
    *,
    schema_columns: Mapping[str, set[str]],
    cte_columns: Mapping[str, set[str]],
    scope_columns_cache: dict[int, dict[str, set[str]]],
    scope_output_cache: dict[int, set[str]],
) -> set[str]:
    cached = scope_output_cache.get(id(scope))
    if cached is not None:
        return cached
    output: set[str] = set()
    scope_output_cache[id(scope)] = output
    source_columns = _discovery_scope_source_columns(
        scope,
        schema_columns=schema_columns,
        cte_columns=cte_columns,
        scope_columns_cache=scope_columns_cache,
        scope_output_cache=scope_output_cache,
    )
    output.update(
        _normalize(name) for name in scope.expression.named_selects if name and name != "*"
    )
    for expression in scope.expression.expressions:
        if isinstance(expression, exp.Star):
            for columns in source_columns.values():
                output.update(columns)
        elif isinstance(expression, exp.Column) and expression.is_star:
            output.update(source_columns.get(_normalize(expression.table), set()))
    output.update(_normalize(name) for name in scope.outer_columns if name)
    return output


def _discovery_physical_sources(scope: Scope, cte_names: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for alias, source in _discovery_selected_sources(scope):
        if not isinstance(source, exp.Table):
            continue
        source_name = _normalize(source.name)
        if source_name in cte_names:
            continue
        result[_normalize(alias)] = source.name
    return result


def _discovery_selected_sources(scope: Scope) -> tuple[tuple[str, object], ...]:
    """Return only the relations selected by this SELECT/UNION branch.

    SQLGlot keeps all visible CTEs in ``Scope.sources`` while traversing a
    UNION. Looking at that mapping directly makes a bare output column such
    as ``metric`` appear to come from every sibling CTE, even though each
    UNION branch selects from one relation. ``selected_sources`` is the
    branch-aware projection and still preserves real JOIN multiplicity.
    """

    selected = getattr(scope, "selected_sources", None)
    if isinstance(selected, Mapping) and selected:
        result: list[tuple[str, object]] = []
        for alias, value in selected.items():
            if isinstance(value, tuple) and len(value) >= 2:
                result.append((str(alias), value[1]))
            elif isinstance(value, tuple) and value:
                result.append((str(alias), value[0]))
            else:
                result.append((str(alias), value))
        return tuple(result)
    return tuple((str(alias), source) for alias, source in scope.sources.items())


def _is_scope_alias_reference(column: exp.Column, select_aliases: set[str]) -> bool:
    return (
        isinstance(column.parent, (exp.Ordered, exp.Group))
        and _normalize(column.name) in select_aliases
    )


def _append_discovery_raw_scope_findings(
    findings: list[AnalysisValidationFinding],
    *,
    statement: exp.Expression,
    schema_columns: Mapping[str, set[str]],
    allowed_columns_by_table: Mapping[str, set[str]],
    cte_names: set[str],
) -> None:
    """保留 SQLGlot 无法定资格时的精确物理字段诊断。"""

    scope_columns_cache: dict[int, dict[str, set[str]]] = {}
    scope_output_cache: dict[int, set[str]] = {}
    for scope in traverse_scope(statement):
        source_columns = _discovery_scope_source_columns(
            scope,
            schema_columns=schema_columns,
            cte_columns=_discovery_cte_columns(statement),
            scope_columns_cache=scope_columns_cache,
            scope_output_cache=scope_output_cache,
        )
        physical_sources = _discovery_physical_sources(scope, cte_names)
        select_aliases = {_normalize(name) for name in scope.expression.named_selects if name}
        for column in scope.columns:
            if column.is_star:
                continue
            name = _normalize(column.name)
            qualifier = _normalize(column.table) if column.table else ""
            if not qualifier and _is_scope_alias_reference(column, select_aliases):
                continue
            if qualifier:
                available = source_columns.get(qualifier)
                if available is None or name not in available:
                    _append_unique_finding(
                        findings,
                        _finding(
                            "DISCOVERY_SCOPE_COLUMN_INVALID",
                            f"Discovery SQL 引用了作用域中不存在的字段 {column.sql()}。",
                        ),
                    )
                    continue
                physical_table = physical_sources.get(qualifier)
                if physical_table is not None and name not in allowed_columns_by_table.get(
                    _normalize(physical_table), set()
                ):
                    _append_unique_finding(
                        findings,
                        _finding(
                            "DISCOVERY_SCOPE_COLUMN_FORBIDDEN",
                            f"Discovery SQL 使用了不在探索白名单中的字段 {column.sql()}。",
                        ),
                    )
                continue
            candidates = [alias for alias, columns in source_columns.items() if name in columns]
            if len(candidates) != 1:
                code = (
                    "DISCOVERY_SCOPE_COLUMN_AMBIGUOUS"
                    if len(candidates) > 1
                    else "DISCOVERY_SCOPE_COLUMN_INVALID"
                )
                _append_unique_finding(
                    findings,
                    _finding(
                        code,
                        f"Discovery SQL 的裸字段 {column.sql()} 无法在当前作用域中唯一确定。",
                    ),
                )
                continue
            physical_table = physical_sources.get(candidates[0])
            if physical_table is not None and name not in allowed_columns_by_table.get(
                _normalize(physical_table), set()
            ):
                _append_unique_finding(
                    findings,
                    _finding(
                        "DISCOVERY_SCOPE_COLUMN_FORBIDDEN",
                        f"Discovery SQL 使用了不在探索白名单中的字段 {column.sql()}。",
                    ),
                )


def _discovery_column_is_derived_output(
    scope: Scope,
    column: exp.Column,
    *,
    schema_columns: Mapping[str, set[str]],
    cte_columns: Mapping[str, set[str]],
    cte_names: set[str],
    scope_columns_cache: dict[int, dict[str, set[str]]],
    scope_output_cache: dict[int, set[str]],
) -> bool:
    """判断无法被 SqlScopeIndex 回溯的列是否仍是合法派生 relation 输出。"""

    name = _normalize(column.name)
    qualifier = _normalize(column.table) if column.table else ""
    if not qualifier and _is_scope_alias_reference(
        column,
        {_normalize(value) for value in scope.expression.named_selects if value},
    ):
        return True
    current: Scope | None = scope
    if qualifier:
        while current is not None:
            source = current.sources.get(qualifier)
            if source is not None:
                if not isinstance(source, Scope):
                    return False
                return name in _discovery_scope_output_columns(
                    source,
                    schema_columns=schema_columns,
                    cte_columns=cte_columns,
                    scope_columns_cache=scope_columns_cache,
                    scope_output_cache=scope_output_cache,
                )
            current = current.parent
        return False
    derived_matches = 0
    current = scope
    while current is not None:
        for _, source in _discovery_selected_sources(current):
            if not isinstance(source, Scope):
                continue
            if name in _discovery_scope_output_columns(
                source,
                schema_columns=schema_columns,
                cte_columns=cte_columns,
                scope_columns_cache=scope_columns_cache,
                scope_output_cache=scope_output_cache,
            ):
                derived_matches += 1
        current = current.parent
    return derived_matches == 1


def _scope_lookup(statement: exp.Expression) -> dict[int, Scope]:
    """把 AST 字段节点绑定到 SQLGlot 作用域，统一处理 alias/CTE。"""

    result: dict[int, Scope] = {}
    for scope in traverse_scope(statement):
        for column in scope.columns:
            result[id(column)] = scope
    return result


def _scope_table_sources(scope: Scope | None) -> dict[str, str]:
    if scope is None:
        return {}
    result: dict[str, str] = {}
    for alias, source in scope.sources.items():
        if isinstance(source, exp.Table):
            result[_normalize(alias)] = source.name
            result[_normalize(source.name)] = source.name
    return result


def _schema_columns(schema: SchemaSummaryRead | None) -> dict[str, set[str]]:
    if schema is None:
        return {}
    return {
        _normalize(table.name): {_normalize(column.name) for column in table.columns}
        for table in schema.tables
    }


def _resolve_column_reference(
    column: exp.Column,
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None = None,
) -> _SqlColumnReference:
    """解析 AST 字段的真实表；无法唯一确定时保留 table=None。"""

    if scope_index is not None:
        lineage = scope_index.column_lineage(column, scope_lookup.get(id(column)))
        physical = tuple(
            (_normalize(item.table), _normalize(item.column)) for item in lineage.physical
        )
        table: str | None = None
        if len(physical) == 1:
            table = physical[0][0]
        return _SqlColumnReference(
            table=table,
            column=column.name,
            alias=column.table or None,
            physical=physical,
        )

    scope = scope_lookup.get(id(column))
    source_aliases = _scope_table_sources(scope)
    qualifier = _normalize(column.table) if column.table else ""
    if qualifier:
        return _SqlColumnReference(
            table=source_aliases.get(qualifier, qualifier),
            column=column.name,
            alias=column.table or None,
        )
    candidates = [
        table_name
        for table_name in source_aliases.values()
        if schema is None
        or _normalize(column.name) in _schema_columns(schema).get(_normalize(table_name), set())
    ]
    unique_candidates = {_normalize(value): value for value in candidates}
    table = next(iter(unique_candidates.values())) if len(unique_candidates) == 1 else None
    return _SqlColumnReference(table=table, column=column.name, alias=column.table or None)


def _collect_column_references(
    statement: exp.Expression,
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None = None,
) -> list[_SqlColumnReference]:
    result: list[_SqlColumnReference] = []
    for scope in traverse_scope(statement):
        for column in scope.columns:
            if column.is_star:
                continue
            result.append(
                _resolve_column_reference(
                    column,
                    schema=schema,
                    scope_lookup=scope_lookup,
                    scope_index=scope_index,
                )
            )
    return result


def _collect_expression_references(
    expressions: Sequence[exp.Expression],
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None = None,
) -> list[_SqlColumnReference]:
    result: list[_SqlColumnReference] = []
    for expression in expressions:
        for column in expression.find_all(exp.Column):
            result.append(
                _resolve_column_reference(
                    column,
                    schema=schema,
                    scope_lookup=scope_lookup,
                    scope_index=scope_index,
                )
            )
    return result


def _collect_projection_references(
    statement: exp.Expression,
    *,
    schema: SchemaSummaryRead | None,
    scope_lookup: Mapping[int, Scope],
    scope_index: SqlScopeIndex | None = None,
) -> list[_SqlColumnReference]:
    result: list[_SqlColumnReference] = []
    for expression in statement.expressions:
        projected = expression.this if isinstance(expression, exp.Alias) else expression
        if not isinstance(projected, exp.Column):
            continue
        reference = _resolve_column_reference(
            projected,
            schema=schema,
            scope_lookup=scope_lookup,
            scope_index=scope_index,
        )
        result.append(
            _SqlColumnReference(
                table=reference.table,
                column=reference.column,
                alias=expression.alias if isinstance(expression, exp.Alias) else None,
                expression_type="result_projection",
                physical=reference.physical,
            )
        )
    return result


def _assertion_result_names(assertion: AnalysisAssertion) -> set[str]:
    return {_normalize(value) for value in assertion.result_columns} | {
        _normalize(constraint.alias)
        for constraint in assertion.sql_constraints
        if constraint.kind == "aggregate" and constraint.alias
    }


def _is_qualified_reference(value: str) -> bool:
    return isinstance(value, str) and value.strip().count(".") == 1


def _physical_identity(
    value: object,
    *,
    schema: SchemaSummaryRead | None,
    allowed_tables: Sequence[str],
) -> tuple[str | None, str] | None:
    if not isinstance(value, str) or not value.strip() or value == "*":
        return None
    if schema is not None:
        try:
            reference = parse_physical_reference(
                schema,
                value,
                allowed_tables=allowed_tables or None,
            )
        except PhysicalReferenceError:
            return None
        return _normalize(reference.table), _normalize(reference.column)
    parts = [part.strip('"`[] ') for part in value.strip().split(".")]
    if len(parts) == 2 and all(parts):
        return _normalize(parts[0]), _normalize(parts[1])
    if len(parts) == 1 and parts[0]:
        return None, _normalize(parts[0])
    return None


def _column_identity_matches(
    observed_table: str | None,
    observed_column: str,
    expected_value: str,
    expected_identity: tuple[str | None, str] | None,
    allowed_tables: Sequence[str],
) -> bool:
    if expected_identity is None:
        return False
    expected_table, expected_column = expected_identity
    if _normalize(observed_column) != expected_column:
        return False
    if expected_table is not None:
        return observed_table is not None and _normalize(observed_table) == expected_table
    if observed_table is None:
        return True
    return not allowed_tables or _normalize(observed_table) in {
        _normalize(table) for table in allowed_tables
    }


def _column_reference_matches(
    references: Iterable[_SqlColumnReference],
    expected: str,
    *,
    schema: SchemaSummaryRead | None,
    allowed_tables: Sequence[str],
) -> bool:
    expected_identity = _physical_identity(
        expected,
        schema=schema,
        allowed_tables=allowed_tables,
    )
    for reference in references:
        if reference.physical:
            if any(
                _column_identity_matches(
                    table,
                    column,
                    expected,
                    expected_identity,
                    allowed_tables,
                )
                for table, column in reference.physical
            ):
                return True
            continue
        if _column_identity_matches(
            reference.table,
            reference.column,
            expected,
            expected_identity,
            allowed_tables,
        ):
            return True
    return False


def _aggregate_column_matches(
    aggregate: _Aggregate,
    expected_column: object,
    expected_identity: tuple[str | None, str] | None,
    allowed_tables: Sequence[str],
) -> bool:
    if expected_column is None:
        return True
    if expected_column == "*":
        return aggregate.column == "*"
    return _column_identity_matches(
        aggregate.table,
        aggregate.column or "",
        str(expected_column),
        expected_identity,
        allowed_tables,
    )


def _format_expected_aggregate(constraint: object) -> str:
    function = str(getattr(constraint, "function", "")).upper()
    column = getattr(constraint, "column", None)
    alias = getattr(constraint, "alias", None)
    text = f"{function}({column or '*'})"
    return f"{text} AS {alias}" if alias else text


def _format_aggregate(aggregate: _Aggregate) -> str:
    if aggregate.distinct:
        argument = aggregate.column or "..."
        text = f"{aggregate.function.upper()}(DISTINCT {argument})"
    else:
        argument = (
            f"{aggregate.table}.{aggregate.column}"
            if aggregate.table and aggregate.column not in {None, "*"}
            else (aggregate.column or "*")
        )
        text = f"{aggregate.function.upper()}({argument})"
    return f"{text} AS {aggregate.alias}" if aggregate.alias else text


def _finding(code: str, message: str, assertion_id: str | None = None) -> AnalysisValidationFinding:
    return AnalysisValidationFinding(
        code=code, message=message, severity="error", assertion_id=assertion_id
    )


def _append_unique_finding(
    findings: list[AnalysisValidationFinding], finding: AnalysisValidationFinding
) -> None:
    if any(
        existing.code == finding.code and existing.message == finding.message
        for existing in findings
    ):
        return
    findings.append(finding)


def _normalize(value: str) -> str:
    return value.strip('"`[]').casefold()


def _column_name(value: str) -> str:
    """返回物理限定字段或 SQL AST 字段的叶子名。"""

    return _normalize(value.rsplit(".", 1)[-1])


def _parser_dialect(dialect: str | None) -> str | None:
    normalized = (dialect or "").casefold()
    return (
        normalized if normalized in {"sqlite", "duckdb", "postgres", "mysql", "bigquery"} else None
    )
