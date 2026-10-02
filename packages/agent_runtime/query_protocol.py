"""SQL 查询的执行前确定性校验与声明侧结果列派生。

大切换后这里只保留两类检查：Discovery 的物理 allowlist 预检，和正式查询
assertion 声明的物理来源解析（安全边界）。SQL 与合同的匹配（聚合/分组/谓词/
来源/结果列）已整体移除：候选 SQL 的结构事实由 ``contract_derivation`` 推导，
推导不出时放松核对而不是拒绝；SQL 语法、只读性和物理字段错误由 Data Gateway
SQL Guard 所有。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from contracts.datasources import SchemaSummaryRead
from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from agent_runtime.contracts import (
    AnalysisAssertion,
    AnalysisValidationFinding,
)
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
    """核对 assertion 声明的物理来源，并派生声明侧回退结果列。

    大切换后这里不再比较 SQL 与合同：聚合/分组/谓词/来源使用/结果列的
    SQL_CONTRACT_* 拒绝逻辑全部移除。候选 SQL 的结构事实由
    ``contract_derivation.derive_contract`` 推导，推导不出时放松核对而不是拒绝；
    SQL 语法、只读性和物理字段错误仍由 Data Gateway SQL Guard 所有。保留的
    安全边界是：声明读取的 source_tables 必须是当前冻结 Schema 中的真实表。
    """

    del sql, dialect
    expected_columns = authoritative_result_columns(assertions)
    findings: list[AnalysisValidationFinding] = []
    if schema is not None:
        for assertion in assertions:
            try:
                for table in assertion.source_tables:
                    parse_physical_reference(schema, table, require_column=False)
            except PhysicalReferenceError as exc:
                findings.append(
                    _finding("SQL_CONTRACT_PHYSICAL_REFERENCE_INVALID", str(exc), assertion.id)
                )
    return _result(findings, expected_columns)


def _result(
    findings: list[AnalysisValidationFinding],
    expected_columns: list[str],
) -> SqlContractPreflight:
    return SqlContractPreflight(
        valid=not findings, findings=findings, expected_columns=expected_columns
    )


def authoritative_result_columns(assertions: Sequence[AnalysisAssertion]) -> list[str]:
    """从声明派生回退用的结果列；正常路径的结果列来自实际 SQL 投影推导。

    ``contract_derivation.derive_contract`` 是 ``expected_columns`` 的第一来源；
    这里只是 ``derived_ok=False`` 时的声明回退。claim 字段允许模型填写物理全名
    （如 ``customers.region``），输出时去掉物理限定只保留叶子别名，避免把物理
    引用当成结果列去核对投影（q11 误拒根因）。
    """

    result: list[str] = []
    seen: set[str] = set()
    for assertion in assertions:
        for column in assertion.result_columns:
            _append_column(result, seen, column)
        for extraction in assertion.claim_extractions:
            if extraction.mode == "scalar":
                _append_column(result, seen, _alias_name(extraction.field))
                for selector_key in extraction.selector or {}:
                    _append_column(result, seen, _alias_name(selector_key))
            else:
                _append_column(result, seen, _alias_name(extraction.value_field))
                for column in extraction.dimension_fields:
                    _append_column(result, seen, _alias_name(column))
        for dimension in assertion.dimensions:
            _append_column(result, seen, _alias_name(dimension))
    return result


def _alias_name(value: str) -> str:
    """把声明侧字段引用归一为结果别名：物理限定（table.column）取叶子名。"""

    stripped = value.strip()
    if stripped.count(".") == 1:
        leaf = stripped.rsplit(".", 1)[-1].strip()
        if leaf:
            return leaf
    return stripped


def _append_column(result: list[str], seen: set[str], column: str) -> None:
    normalized = _normalize(column)
    if normalized in seen:
        return
    seen.add(normalized)
    result.append(column)


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


def _parser_dialect(dialect: str | None) -> str | None:
    normalized = (dialect or "").casefold()
    return (
        normalized if normalized in {"sqlite", "duckdb", "postgres", "mysql", "bigquery"} else None
    )
