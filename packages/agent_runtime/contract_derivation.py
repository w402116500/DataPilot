"""候选 SQL 的纯结构推导器（永不拦截）。

这个模块只回答三个事实问题：SQL 实际读取了哪些物理表、顶层 SELECT 输出了
哪些列名、每个投影列能追溯到哪些物理 ``表.列``。它不执行 SQL、不做安全判断、
不生成业务预期值；解析失败或遇到推导能力之外的结构（窗口函数、UNION 分支、
不可展开的 ``*`` 等）时返回部分结果并置 ``derived_ok=False``，绝不抛出异常、
绝不产生拦截性 finding。SQL 是否安全、能否执行仍由 Guard / Gateway / SQL
Audit 等既有所有者裁决。

与 ``sql_scope.SqlScopeIndex`` 的关系：提供 Schema 且 qualification 成功时，
复用同一 qualified Scope 索引做字段 lineage（``schema_verified=True``）；未提供
Schema 或 qualification 失败时退回纯 AST 作用域推导，结果带
``schema_qualification_unavailable`` 降级标记（限定名字段仍可追溯，裸字段只在
无歧义时追溯）。窗口函数无论写在顶层投影还是藏在 CTE/派生表内部，凡被输出
投影（经传递）触及的都标记 derived，且不把窗口内部分列当成物理来源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from contracts.datasources import SchemaSummaryRead
from sqlglot import exp, parse
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, find_all_in_scope, traverse_scope

from agent_runtime.sql_scope import PhysicalColumn, SqlScopeBuildError, SqlScopeIndex

ProjectionStatus = Literal["resolved", "derived"]

# sqlglot qualify 会给无别名投影生成 ``_col_N`` 占位名；真实结果列名依赖引擎，
# 不能把占位名当作可引用的结果列。
_GENERATED_COLUMN_NAME = re.compile(r"_col_\d+$")

# CTE/派生表存在多个同名输出列时无法唯一追溯。
_AMBIGUOUS_OUTPUT = object()


@dataclass(frozen=True)
class DerivedColumn:
    """一个顶层投影列及其可证明的物理来源。

    ``status="resolved"`` 表示表达式完整追溯到 ``sources`` 列出的物理
    ``表.列``（是否经 Schema 校验由外层 ``schema_verified`` 表达）；
    ``status="derived"`` 表示存在追溯不到的部分。``sources=()`` 有两种互斥
    含义：与 ``resolved`` 同现表示该投影不引用任何物理列（如 ``COUNT(*)``、
    字面量）；与 ``derived`` 同现表示来源未知，二者不可混读。
    """

    name: str
    status: ProjectionStatus
    sources: tuple[PhysicalColumn, ...] = ()


@dataclass(frozen=True)
class ContractDerivation:
    """一次候选 SQL 结构推导的不可变结果。

    ``derived_ok=False`` 表示结构未完整推导（解析失败、窗口函数、UNION、
    裸字段歧义、Schema qualification 失败等），此时 ``unsupported`` 给出可定位
    的降级原因；调用方必须把降级当作"放松核对"，不能当作拒绝 SQL 的理由。

    ``schema_verified=True`` 表示 lineage 已通过传入 Schema 的 qualification
    校验；``False`` 时 ``sources`` 只是 SQL 文本的结构推导（未传 Schema，或
    传了但 qualification 失败）。下游把 ``resolved`` 来源当作已验证事实前，
    必须先确认 ``schema_verified``。
    """

    derived_ok: bool
    physical_tables: tuple[str, ...]
    projections: tuple[DerivedColumn, ...]
    unsupported: tuple[str, ...] = ()
    schema_verified: bool = False


@dataclass
class _Context:
    """一次推导的共享解析上下文。"""

    index: SqlScopeIndex | None
    cte_names: frozenset[str]
    schema_columns: dict[str, frozenset[str]]
    scope_by_id: dict[int, Scope]


def derive_contract(
    sql: str,
    *,
    dialect: str | None = None,
    schema: SchemaSummaryRead | None = None,
) -> ContractDerivation:
    """从候选 SQL 推导结构事实；任何失败都降级为部分结果，不抛异常。

    ``schema`` 可选：提供且 qualification 成功时给出 Schema 校验过的 lineage，
    并展开 ``*`` 投影；qualification 失败时整体降级（不假装已校验）；缺省时
    仅做结构推导，无别名的非列投影名与 ``*`` 展开结果无法确定。
    """

    try:
        return _derive(sql, dialect=dialect, schema=schema)
    except Exception:
        # 推导器铁律：永不拦截。未预期异常同样降级为"未推导"，
        # 提取器自身的失败绝不能变成对候选 SQL 的拒绝。
        return ContractDerivation(False, (), (), ("derivation_error",))


def _derive(
    sql: str, *, dialect: str | None, schema: SchemaSummaryRead | None
) -> ContractDerivation:
    parser_dialect = _parser_dialect(dialect)
    try:
        statements = parse(sql, read=parser_dialect)
    except SqlglotError:
        return ContractDerivation(False, (), (), ("parse_error",))
    statements = [statement for statement in statements if statement is not None]
    if not statements:
        return ContractDerivation(False, (), (), ("empty_statement",))
    if len(statements) > 1:
        return ContractDerivation(False, (), (), ("multiple_statements",))
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        return ContractDerivation(False, (), (), ("non_query_statement",))

    schema_verified = False
    index: SqlScopeIndex | None = None
    working = statement
    if schema is not None:
        try:
            index = SqlScopeIndex(statement, schema, parser_dialect or dialect or "")
            working = index.statement
            schema_verified = True
        except (SqlScopeBuildError, SqlglotError):
            index = None
    # Schema 在手但 qualification 失败：字段来源退回纯文本推导、未经 Schema
    # 校验，必须整体降级，避免把回退路径的 resolved 当成已验证事实。
    fallback_reasons: tuple[str, ...] = ()
    if schema is not None and not schema_verified:
        fallback_reasons = ("schema_qualification_unavailable",)

    cte_names = _cte_names(working)
    scopes = list(traverse_scope(working))
    root = next((scope for scope in scopes if scope.parent is None), None)
    tables = _physical_tables(working, cte_names)
    if root is None:
        return ContractDerivation(
            False, tables, (), fallback_reasons + ("scope_error",), schema_verified
        )
    context = _Context(
        index=index,
        cte_names=cte_names,
        schema_columns=_schema_columns(schema),
        scope_by_id={id(scope.expression): scope for scope in scopes},
    )
    if isinstance(root.expression, exp.SetOperation):
        # UNION/EXCEPT/INTERSECT 的输出列名来自首个分支，但行语义无法在不
        # 执行的情况下归并；标记未推导并给出首个分支的列名作为部分结果。
        projections = tuple(
            DerivedColumn(name, "derived") for name in _set_operation_names(root.expression)
        )
        return ContractDerivation(
            False, tables, projections, fallback_reasons + ("set_operation",), schema_verified
        )

    projections: list[DerivedColumn] = []
    unsupported: list[str] = list(fallback_reasons)
    for raw_expression, expression in _projection_pairs(statement, root.expression):
        name = _projection_name(raw_expression, expression)
        target = expression.this if isinstance(expression, exp.Alias) else expression
        if _is_star(target):
            projections.append(DerivedColumn(name or "*", "derived"))
            _append_reason(unsupported, "star_unexpanded")
            continue
        status, sources, reasons = _resolve_expression(context, target, root, frozenset())
        if not name:
            status = "derived"
            reasons.append("unnamed_projection")
        if reasons:
            status = "derived"
        projections.append(DerivedColumn(name, status, sources))
        for reason in reasons:
            _append_reason(unsupported, reason)
    return ContractDerivation(
        derived_ok=not unsupported and all(item.status == "resolved" for item in projections),
        physical_tables=tables,
        projections=tuple(projections),
        unsupported=tuple(unsupported),
        schema_verified=schema_verified,
    )


def _resolve_expression(
    context: _Context,
    expression: exp.Expression,
    scope: Scope,
    visiting: frozenset[int],
) -> tuple[ProjectionStatus, tuple[PhysicalColumn, ...], list[str]]:
    """解析一个投影表达式：本作用域字段、CTE/派生表传递与嵌套子查询的来源。"""

    if expression.find(exp.Window):
        # 窗口输出的值语义（分区/排序）不等于其内部列的物理来源；
        # 不给出可能误导的 lineage，交给降级标记。
        return "derived", (), ["window_function"]

    reasons: list[str] = []
    sources: dict[tuple[str, str], PhysicalColumn] = {}
    status: ProjectionStatus = "resolved"

    def merge(
        column_status: ProjectionStatus,
        column_sources: tuple[PhysicalColumn, ...],
        column_reasons: list[str],
    ) -> None:
        nonlocal status
        if column_status != "resolved":
            status = "derived"
        for item in column_sources:
            sources.setdefault((_normalize(item.table), _normalize(item.column)), item)
        reasons.extend(column_reasons)

    for column in find_all_in_scope(expression, exp.Column):
        merge(*_resolve_column(context, column, scope, visiting))

    for select_node in expression.find_all(exp.Select):
        inner_scope = context.scope_by_id.get(id(select_node))
        if inner_scope is None:
            status = "derived"
            _append_reason(reasons, "unresolved_projection")
            continue
        for inner_expression in inner_scope.expression.expressions:
            inner_target = (
                inner_expression.this
                if isinstance(inner_expression, exp.Alias)
                else inner_expression
            )
            if _is_star(inner_target):
                status = "derived"
                _append_reason(reasons, "star_unexpanded")
                continue
            merge(
                *_resolve_expression(
                    context, inner_target, inner_scope, visiting | {id(inner_scope)}
                )
            )
    return status, tuple(sources.values()), reasons


def _resolve_column(
    context: _Context,
    column: exp.Column,
    scope: Scope,
    visiting: frozenset[int],
) -> tuple[ProjectionStatus, tuple[PhysicalColumn, ...], list[str]]:
    """解析单个字段：限定名直接找来源，裸字段在作用域链中找唯一候选。"""

    qualifier = _normalize(column.table) if column.table else ""
    if qualifier:
        source = _find_source(scope, qualifier)
        if isinstance(source, Scope):
            return _resolve_through_scope(context, source, column.name, visiting)
    else:
        source, ambiguous = _unique_column_source(context, column, scope)
        if ambiguous:
            return "derived", (), ["ambiguous_projection"]
        if isinstance(source, Scope):
            return _resolve_through_scope(context, source, column.name, visiting)

    if context.index is not None:
        lineage = context.index.column_lineage(column, scope)
        if lineage.status == "ambiguous":
            return "derived", (), ["ambiguous_projection"]
        if lineage.status != "resolved":
            return "derived", (), ["unresolved_projection"]
        return "resolved", lineage.physical, ()
    if isinstance(source, exp.Table):
        if _normalize(source.name) in context.cte_names:
            # 递归 CTE 自引用等场景：引用落回 CTE 自身，无法继续追溯。
            return "derived", (), ["recursive_reference"]
        return "resolved", (PhysicalColumn(source.name, column.name),), ()
    return "derived", (), ["unresolved_projection"]


def _resolve_through_scope(
    context: _Context,
    source: Scope,
    name: str,
    visiting: frozenset[int],
) -> tuple[ProjectionStatus, tuple[PhysicalColumn, ...], list[str]]:
    """沿 CTE/派生表输出向内追溯；内层窗口、星号与重名输出在此暴露。"""

    if id(source) in visiting:
        return "derived", (), ["recursive_reference"]
    projection = _scope_output_projection(source, name)
    if projection is _AMBIGUOUS_OUTPUT:
        return "derived", (), ["ambiguous_projection"]
    if projection is None:
        return "derived", (), ["unresolved_projection"]
    target = projection.this if isinstance(projection, exp.Alias) else projection
    if _is_star(target):
        return "derived", (), ["star_unexpanded"]
    return _resolve_expression(context, target, source, visiting | {id(source)})


def _unique_column_source(
    context: _Context, column: exp.Column, scope: Scope
) -> tuple[object | None, bool]:
    """在作用域链中定位裸字段唯一可能的来源；返回 (来源, 是否歧义)。"""

    current: Scope | None = scope
    while current is not None:
        candidates = [
            item
            for item in current.sources.values()
            if _source_has_column(context, item, column.name)
        ]
        if len(candidates) == 1:
            return candidates[0], False
        if len(candidates) > 1:
            return None, True
        current = current.parent
    return None, False


def _source_has_column(context: _Context, source: object, name: str) -> bool:
    """判断一个来源是否可能输出该字段；不确定时保守返回 True。"""

    if isinstance(source, exp.Table):
        if _normalize(source.name) in context.cte_names:
            return False
        columns = context.schema_columns.get(_normalize(source.name))
        if columns is None:
            return not context.schema_columns
        return _normalize(name) in columns
    if isinstance(source, Scope):
        wanted = _normalize(name)
        for expression in source.expression.expressions:
            if _is_star(expression):
                return True
            output = expression.alias_or_name
            if output and _normalize(output) == wanted:
                return True
        return False
    return False


def _scope_output_projection(scope: Scope, name: str) -> exp.Expression | object | None:
    """按输出名找一个 CTE/派生表投影；重名输出返回歧义哨兵。"""

    wanted = _normalize(name)
    found: exp.Expression | None = None
    for expression in scope.expression.expressions:
        if _is_star(expression):
            continue
        output = expression.alias_or_name
        if output and _normalize(output) == wanted:
            if found is not None:
                return _AMBIGUOUS_OUTPUT
            found = expression
    return found


def _projection_pairs(
    statement: exp.Expression, root_expression: exp.Expression
) -> list[tuple[exp.Expression | None, exp.Expression]]:
    """配对原始投影与 qualified 投影；qualify 展开 ``*`` 后放弃原始配对。"""

    qualified = root_expression.expressions
    raw = statement.expressions if isinstance(statement, exp.Select) else None
    if raw is not None and len(raw) == len(qualified):
        return list(zip(raw, qualified, strict=True))
    return [(None, expression) for expression in qualified]


def _projection_name(raw_expression: exp.Expression | None, expression: exp.Expression) -> str:
    if isinstance(raw_expression, exp.Alias):
        return raw_expression.alias or ""
    if isinstance(raw_expression, exp.Column) and not raw_expression.is_star:
        return raw_expression.name
    if raw_expression is None:
        name = expression.alias_or_name
        if name and not _GENERATED_COLUMN_NAME.fullmatch(name):
            return name
    # 无别名的非列投影（COUNT(*)、字面量等）结果列名依赖引擎，不猜。
    return ""


def _set_operation_names(expression: exp.Expression) -> list[str]:
    current: exp.Expression = expression
    while isinstance(current, exp.SetOperation):
        current = current.left
    names: list[str] = []
    if isinstance(current, exp.Select):
        for item in current.expressions:
            if _is_star(item):
                continue
            name = item.alias_or_name
            if name and not _GENERATED_COLUMN_NAME.fullmatch(name):
                names.append(name)
    return names


def _physical_tables(statement: exp.Expression, cte_names: frozenset[str]) -> tuple[str, ...]:
    """按出现顺序返回物理表名（归一化），CTE 引用不算物理表。"""

    result: list[str] = []
    seen: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = _normalize(table.name)
        if name and name not in cte_names and name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _cte_names(statement: exp.Expression) -> frozenset[str]:
    return frozenset(
        _normalize(cte.alias_or_name) for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    )


def _schema_columns(schema: SchemaSummaryRead | None) -> dict[str, frozenset[str]]:
    if schema is None:
        return {}
    return {
        _normalize(table.name): frozenset(_normalize(column.name) for column in table.columns)
        for table in schema.tables
    }


def _is_star(expression: exp.Expression) -> bool:
    return isinstance(expression, exp.Star) or (
        isinstance(expression, exp.Column) and expression.is_star
    )


def _find_source(scope: Scope, qualifier: str) -> object | None:
    current: Scope | None = scope
    while current is not None:
        for alias, source in current.sources.items():
            if _normalize(alias) == qualifier:
                return source
        current = current.parent
    return None


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _normalize(value: str | None) -> str:
    return (value or "").strip('"`[] ').casefold()


def _parser_dialect(dialect: str | None) -> str | None:
    """与 query_protocol._parser_dialect 保持同一白名单。

    抽公共 helper 需要改动 query_protocol.py，超出本步"不接线"边界；
    第 3 步大切换触碰 query_protocol 时应合并为单一 helper。
    """

    normalized = (dialect or "").casefold()
    return (
        normalized if normalized in {"sqlite", "duckdb", "postgres", "mysql", "bigquery"} else None
    )
