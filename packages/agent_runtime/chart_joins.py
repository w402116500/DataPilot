"""Confirmed joins are the intersection of executed SQL and already confirmed keys."""

from __future__ import annotations

from dataclasses import dataclass

from contracts.datalink import DataLinkSemanticContext
from contracts.datasources import SchemaSummaryRead
from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope

from agent_runtime.sql_scope import PhysicalColumn, SqlScopeBuildError, SqlScopeIndex, _key

_CONFIRMED_PROVENANCE = {"database_foreign_key", "structural", "manual"}
_DIALECTS = {"sqlite", "duckdb", "postgres", "mysql", "bigquery"}
_PARSE_LIMITATION = "实际查询的 Join 未能核对。"
_UNCONFIRMED_LIMITATION = "查询中的连接未对应到已确认关系。"
_PairKey = tuple[tuple[str, str], tuple[str, str]]


@dataclass(frozen=True)
class ConfirmedJoinProjection:
    """Labels that survived both the SQL predicate and a confirmed key."""

    labels: tuple[str, ...]
    limitation: str | None


def confirmed_join_labels(
    sql: str | None,
    schema: SchemaSummaryRead | None,
    semantic: DataLinkSemanticContext | None,
    dialect: str | None,
) -> ConfirmedJoinProjection:
    """Project column-equality joins that match schema or confirmed foreign keys."""

    if not sql or schema is None:
        return ConfirmedJoinProjection((), _PARSE_LIMITATION)
    normalized = (dialect or schema.dialect or "").casefold()
    if normalized not in _DIALECTS:
        return ConfirmedJoinProjection((), _PARSE_LIMITATION)
    try:
        statements = [item for item in parse(sql, dialect=normalized) if item is not None]
    except ParseError:
        return ConfirmedJoinProjection((), _PARSE_LIMITATION)
    if len(statements) != 1:
        return ConfirmedJoinProjection((), _PARSE_LIMITATION)
    try:
        index = SqlScopeIndex(statements[0], schema, normalized)
    except SqlScopeBuildError:
        return ConfirmedJoinProjection((), _PARSE_LIMITATION)
    observed = _observed_pairs(index)
    if not observed:
        return ConfirmedJoinProjection((), _PARSE_LIMITATION)
    confirmed = _confirmed_pairs(schema, semantic)
    labels = tuple(
        sorted(
            {_label(left, right) for left, right in observed if _pair_key(left, right) in confirmed}
        )
    )[:32]
    if not labels:
        return ConfirmedJoinProjection((), _UNCONFIRMED_LIMITATION)
    return ConfirmedJoinProjection(labels, None)


def _observed_pairs(index: SqlScopeIndex) -> list[tuple[PhysicalColumn, PhysicalColumn]]:
    pairs: list[tuple[PhysicalColumn, PhysicalColumn]] = []
    seen: set[_PairKey] = set()
    for scope in index.scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        clauses: list[exp.Expression] = []
        where = select.args.get("where")
        if isinstance(where, exp.Where) and where.this is not None:
            clauses.append(where.this)
        for join in select.args.get("joins") or []:
            on_clause = join.args.get("on")
            if on_clause is not None:
                clauses.append(on_clause)
        for clause in clauses:
            for comparison in clause.walk():
                if not isinstance(comparison, exp.EQ):
                    continue
                left = _one_column(index, comparison.this, scope)
                right = _one_column(index, comparison.expression, scope)
                if left is None or right is None:
                    continue
                key = _pair_key(left, right)
                if key is None or key in seen:
                    continue
                seen.add(key)
                pairs.append((left, right))
    return pairs


def _one_column(
    index: SqlScopeIndex, expression: exp.Expression, scope: Scope
) -> PhysicalColumn | None:
    lineage = index.expression_lineage(expression, scope)
    if lineage.status != "resolved" or len(lineage.physical) != 1:
        return None
    return lineage.physical[0]


def _confirmed_pairs(
    schema: SchemaSummaryRead, semantic: DataLinkSemanticContext | None
) -> set[_PairKey]:
    confirmed: set[_PairKey] = set()
    for table in schema.tables:
        for foreign_key in table.foreign_keys:
            for column, referenced in zip(
                foreign_key.columns, foreign_key.referenced_columns, strict=False
            ):
                key = _name_pair(table.name, column, foreign_key.referenced_table, referenced)
                if key is not None:
                    confirmed.add(key)
    if semantic is None:
        return confirmed
    for relationship in semantic.relationships:
        if relationship.edge_type != "foreign_key":
            continue
        if relationship.provenance not in _CONFIRMED_PROVENANCE:
            continue
        key = _name_pair(
            relationship.source_table,
            relationship.source_column,
            relationship.target_table,
            relationship.target_column,
        )
        if key is not None:
            confirmed.add(key)
    return confirmed


def _name_pair(
    left_table: str, left_column: str, right_table: str, right_column: str
) -> _PairKey | None:
    left = (_key(left_table), _key(left_column))
    right = (_key(right_table), _key(right_column))
    if left[0] == right[0]:
        return None
    if left <= right:
        return (left, right)
    return (right, left)


def _pair_key(left: PhysicalColumn, right: PhysicalColumn) -> _PairKey | None:
    return _name_pair(left.table, left.column, right.table, right.column)


def _label(left: PhysicalColumn, right: PhysicalColumn) -> str:
    ordered = sorted((left, right), key=lambda item: (_key(item.table), _key(item.column)))
    return f"{ordered[0].table}.{ordered[0].column} = {ordered[1].table}.{ordered[1].column}"
