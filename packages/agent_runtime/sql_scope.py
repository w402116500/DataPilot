"""SQLGlot 作用域和物理字段 lineage 的共享只读索引。

这个模块只回答一个问题：SQL 中看到的字段，最终来自当前 Schema 的哪张物理表。
它不执行 SQL，也不理解 AnalysisAssertion；合同层和 Discovery 层只消费这里的结构化结果。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from contracts.datasources import SchemaSummaryRead
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, find_all_in_scope, traverse_scope

LineageStatus = Literal["resolved", "unknown", "ambiguous"]


@dataclass(frozen=True)
class PhysicalColumn:
    """一个经过 Schema 校验的物理字段。"""

    table: str
    column: str


@dataclass(frozen=True)
class ColumnLineage:
    """一个 SQL 字段节点的解析结果。"""

    status: LineageStatus
    physical: tuple[PhysicalColumn, ...] = ()


@dataclass(frozen=True)
class OutputLineage:
    """派生 relation 对外暴露的一个结果列。"""

    status: LineageStatus
    physical: tuple[PhysicalColumn, ...] = ()


class SqlScopeBuildError(ValueError):
    """SQLGlot 无法把 SQL 字段唯一绑定到当前 Schema。"""

    def __init__(self, message: str, *, defer_to_gateway: bool = False) -> None:
        super().__init__(message)
        self.defer_to_gateway = defer_to_gateway


class SqlScopeIndex:
    """基于 SQLGlot qualified AST 的作用域、别名和物理 lineage 索引。

    ``qualify`` 会先展开 ``*``、解析表别名、CTE/子查询输出和合法的
    ``GROUP BY``/``ORDER BY`` 别名。之后再遍历 Scope，任何物理比较都使用
    ``PhysicalColumn``，不会把派生 relation 的列名误当成物理表字段。
    """

    def __init__(self, statement: exp.Expression, schema: SchemaSummaryRead, dialect: str):
        schema_map = _schema_map(schema)
        try:
            qualified = qualify(
                statement.copy(),
                schema=schema_map,
                dialect=dialect,
                validate_qualify_columns=True,
                quote_identifiers=False,
            )
        except SqlglotError as exc:
            raise SqlScopeBuildError(
                "SQL 字段作用域无法唯一解析。",
                defer_to_gateway=_may_defer_to_gateway(statement, schema),
            ) from exc

        self.statement = qualified
        self._schema_map = schema_map
        self._scope_lookup: dict[int, Scope] = {}
        self._output_cache: dict[int, dict[str, OutputLineage]] = {}
        self._column_cache: dict[int, ColumnLineage] = {}
        for scope in traverse_scope(qualified):
            for column in scope.columns:
                self._scope_lookup[id(column)] = scope

    @property
    def scopes(self) -> tuple[Scope, ...]:
        return tuple(traverse_scope(self.statement))

    def scope_for(self, expression: exp.Expression) -> Scope | None:
        return self._scope_lookup.get(id(expression))

    def column_lineage(self, column: exp.Column, scope: Scope | None = None) -> ColumnLineage:
        cached = self._column_cache.get(id(column))
        if cached is not None:
            return cached
        current_scope = scope or self.scope_for(column)
        if current_scope is None:
            result = ColumnLineage("unknown")
            self._column_cache[id(column)] = result
            return result

        name = _key(column.name)
        qualifier = _key(column.table) if column.table else ""
        source_scope, source = self._find_source(current_scope, qualifier)
        if source is None:
            output = self._outputs(current_scope).get(name)
            result = (
                ColumnLineage(output.status, output.physical)
                if output is not None
                else ColumnLineage("unknown")
            )
        elif isinstance(source, exp.Table):
            physical_table = self._schema_table(source.name)
            if physical_table is None:
                result = ColumnLineage("unknown")
            elif _schema_column(physical_table, column.name, self._schema_map) is None:
                result = ColumnLineage("unknown")
            else:
                physical_column = _schema_column(physical_table, column.name, self._schema_map)
                result = (
                    ColumnLineage(
                        "resolved",
                        (PhysicalColumn(physical_table, physical_column),),
                    )
                    if physical_column is not None
                    else ColumnLineage("unknown")
                )
        elif isinstance(source, Scope):
            output = self._outputs(source).get(name)
            result = (
                ColumnLineage(output.status, output.physical)
                if output is not None
                else ColumnLineage("unknown")
            )
        else:
            result = ColumnLineage("unknown")

        self._column_cache[id(column)] = result
        return result

    def expression_lineage(self, expression: exp.Expression, scope: Scope) -> ColumnLineage:
        """解析一个投影/谓词表达式中的直接字段，不越过嵌套子查询。"""

        physical: dict[tuple[str, str], PhysicalColumn] = {}
        status: LineageStatus = "resolved"
        for column in find_all_in_scope(expression, exp.Column):
            lineage = self.column_lineage(column, scope)
            if lineage.status == "ambiguous":
                status = "ambiguous"
            elif lineage.status == "unknown" and status != "ambiguous":
                status = "unknown"
            for item in lineage.physical:
                physical[(_key(item.table), _key(item.column))] = item
        return ColumnLineage(status, tuple(physical.values()))

    def iter_columns(self) -> Iterator[tuple[Scope, exp.Column, ColumnLineage]]:
        for scope in self.scopes:
            for column in scope.columns:
                if column.is_star:
                    continue
                yield scope, column, self.column_lineage(column, scope)

    def physical_tables(self) -> set[str]:
        """返回所有实际读取的物理表，不包含 CTE/子查询 relation。"""

        result: set[str] = set()
        for scope in self.scopes:
            for source in scope.sources.values():
                if not isinstance(source, exp.Table):
                    continue
                table = self._schema_table(source.name)
                if table is not None:
                    result.add(_key(table))
        return result

    def output_lineage(self, scope: Scope, name: str) -> OutputLineage | None:
        return self._outputs(scope).get(_key(name))

    def is_direct_physical_column(self, column: exp.Column, scope: Scope | None = None) -> bool:
        """只有直接来自物理表的字段才能满足直接 aggregate 合同。"""

        current_scope = scope or self.scope_for(column)
        if current_scope is None:
            return False
        qualifier = _key(column.table) if column.table else ""
        _, source = self._find_source(current_scope, qualifier)
        if not isinstance(source, exp.Table):
            return False
        return self.column_lineage(column, current_scope).status == "resolved"

    def _outputs(self, scope: Scope) -> dict[str, OutputLineage]:
        cached = self._output_cache.get(id(scope))
        if cached is not None:
            return cached
        result: dict[str, OutputLineage] = {}
        self._output_cache[id(scope)] = result
        for expression in scope.expression.expressions:
            if isinstance(expression, exp.Alias):
                name = expression.alias
                projected = expression.this
            else:
                name = expression.alias_or_name
                projected = expression
            if not name or name == "*":
                continue
            lineage = self.expression_lineage(projected, scope)
            key = _key(name)
            previous = result.get(key)
            if previous is None:
                result[key] = OutputLineage(lineage.status, lineage.physical)
            else:
                result[key] = OutputLineage("ambiguous", previous.physical + lineage.physical)
        return result

    def _find_source(self, scope: Scope, qualifier: str) -> tuple[Scope | None, object | None]:
        if not qualifier:
            return None, None
        current: Scope | None = scope
        while current is not None:
            source = current.sources.get(qualifier)
            if source is not None:
                return current, source
            current = current.parent
        return None, None

    def _schema_table(self, value: str) -> str | None:
        key = _key(value)
        return next((name for name in self._schema_map if _key(name) == key), None)


def _schema_map(schema: SchemaSummaryRead) -> dict[str, dict[str, str]]:
    return {
        table.name: {column.name: column.type or "TEXT" for column in table.columns}
        for table in schema.tables
    }


def _schema_column(
    table: str, column: str, schema_map: Mapping[str, Mapping[str, str]]
) -> str | None:
    table_columns = next(
        (columns for name, columns in schema_map.items() if _key(name) == _key(table)),
        None,
    )
    if table_columns is None:
        return None
    return next((name for name in table_columns if _key(name) == _key(column)), None)


def _key(value: str | None) -> str:
    return (value or "").strip('"`[] ').casefold()


def _may_defer_to_gateway(statement: exp.Expression, schema: SchemaSummaryRead) -> bool:
    """判断作用域错误是否只是 Gateway 应负责的物理字段错误。

    合同预检不重复实现 Gateway 的 UNKNOWN_COLUMN 反馈（尤其是带特殊字符、
    需要加引号的字段）。但派生 relation 输出不存在、错误 alias 和裸字段歧义
    属于 SQL 作用域错误，必须在进入执行器前拒绝。
    """

    schema_columns = {
        _key(table.name): {_key(column.name) for column in table.columns} for table in schema.tables
    }
    has_derived_relation = any(
        scope.parent is not None
        or any(isinstance(source, Scope) for source in scope.sources.values())
        for scope in traverse_scope(statement)
    )
    saw_physical_unknown = False
    for scope in traverse_scope(statement):
        for column in scope.columns:
            if column.is_star:
                continue
            qualifier = _key(column.table) if column.table else ""
            if qualifier:
                source = _find_raw_source(scope, qualifier)
                if isinstance(source, exp.Table):
                    if _key(column.name) in schema_columns.get(_key(source.name), set()):
                        continue
                    saw_physical_unknown = True
                    continue
                if source is None and not has_derived_relation:
                    saw_physical_unknown = True
                    continue
                return False

            physical_matches = 0
            derived_matches = 0
            for source in _visible_raw_sources(scope):
                if isinstance(source, exp.Table):
                    if _key(column.name) in schema_columns.get(_key(source.name), set()):
                        physical_matches += 1
                    else:
                        saw_physical_unknown = True
                elif isinstance(source, Scope):
                    if _key(column.name) in _raw_output_names(source):
                        derived_matches += 1
                    else:
                        return False
            if physical_matches + derived_matches == 1:
                continue
            # Zero matches with a single physical source is an ordinary
            # UNKNOWN_COLUMN that the Gateway can explain and audit.
            if physical_matches == 0 and derived_matches == 0:
                physical_sources = [
                    source
                    for source in _visible_raw_sources(scope)
                    if isinstance(source, exp.Table)
                ]
                if len(physical_sources) == 1:
                    saw_physical_unknown = True
                    continue
            return False
    return saw_physical_unknown


def _find_raw_source(scope: Scope, qualifier: str) -> object | None:
    current: Scope | None = scope
    while current is not None:
        source = current.sources.get(qualifier)
        if source is not None:
            return source
        current = current.parent
    return None


def _visible_raw_sources(scope: Scope) -> tuple[object, ...]:
    result: dict[str, object] = {}
    current: Scope | None = scope
    while current is not None:
        for alias, source in current.sources.items():
            result.setdefault(_key(alias), source)
        current = current.parent
    return tuple(result.values())


def _raw_output_names(scope: Scope) -> set[str]:
    result = {_key(name) for name in scope.outer_columns if name}
    for expression in scope.expression.expressions:
        name = expression.alias_or_name
        if name and name != "*":
            result.add(_key(name))
    return result
