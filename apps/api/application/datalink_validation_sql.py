"""Generate fixed, identifier-quoted relation-validation SQL from a frozen Schema."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead
from contracts.errors import AppError, ErrorCode
from sqlglot import exp

_INTEGER_TYPES = frozenset({"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT"})
_NUMBER_TYPES = frozenset({"DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL"})
_TEXT_TYPES = frozenset({"TEXT", "VARCHAR", "CHAR", "STRING", "CLOB", "NVARCHAR"})
_DATETIME_TYPES = frozenset({"DATE", "DATETIME", "TIMESTAMP", "TIMESTAMPTZ", "TIME"})
_BOOLEAN_TYPES = frozenset({"BOOLEAN", "BOOL"})


@dataclass(frozen=True)
class RelationValidationEndpoint:
    table: str
    column: str
    sql_type: str


@dataclass(frozen=True)
class RelationValidationPlan:
    direction: str
    endpoint_fingerprint: str
    dialect: str
    source: RelationValidationEndpoint
    target: RelationValidationEndpoint
    statements: tuple[tuple[str, str], ...]


def plan_relation_validation(
    schema: SchemaSummaryRead,
    *,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
    direction: str = "source_to_target",
) -> RelationValidationPlan:
    """Build aggregate SQL against Schema-allowlisted identifiers. Never accepts user SQL."""

    source = _endpoint(schema, source_table, source_column)
    target = _endpoint(schema, target_table, target_column)
    if not _types_compatible(source.sql_type, target.sql_type):
        raise AppError(
            ErrorCode.SCHEMA_TYPE_INCOMPATIBLE,
            "关系两端字段类型不兼容，核验不会隐式转换后比较",
            status_code=409,
            details={
                "source_type": source.sql_type,
                "target_type": target.sql_type,
            },
        )
    fingerprint = hashlib.sha256(
        "\0".join(
            [
                direction,
                source.table,
                source.column,
                _type_family(source.sql_type),
                target.table,
                target.column,
                _type_family(target.sql_type),
            ]
        ).encode()
    ).hexdigest()
    statements = (
        ("source_stats", _source_stats_sql(schema.dialect, source)),
        ("target_stats", _target_stats_sql(schema.dialect, target)),
        ("target_duplicates", _target_duplicate_sql(schema.dialect, target)),
        ("source_unmatched", _source_unmatched_sql(schema.dialect, source, target)),
        ("multiple_match", _multiple_match_sql(schema.dialect, source, target)),
    )
    return RelationValidationPlan(
        direction=direction,
        endpoint_fingerprint=fingerprint,
        dialect=schema.dialect,
        source=source,
        target=target,
        statements=statements,
    )


def _endpoint(
    schema: SchemaSummaryRead, table_name: str, column_name: str
) -> RelationValidationEndpoint:
    table = _table(schema, table_name)
    column = _column(table, column_name)
    return RelationValidationEndpoint(table=table.name, column=column.name, sql_type=column.type)


def _table(schema: SchemaSummaryRead, name: str) -> SchemaTableRead:
    matches = [table for table in schema.tables if table.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise AppError(
            ErrorCode.RELATION_NOT_VALIDATABLE,
            "关系端点不在当前 Schema 白名单中",
            status_code=409,
        )
    return matches[0]


def _column(table: SchemaTableRead, name: str) -> SchemaColumnRead:
    matches = [column for column in table.columns if column.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise AppError(
            ErrorCode.RELATION_NOT_VALIDATABLE,
            "关系端点不在当前 Schema 白名单中",
            status_code=409,
        )
    return matches[0]


def _type_token(sql_type: str) -> str:
    return sql_type.split("(", 1)[0].strip().upper()


def _type_family(sql_type: str) -> str:
    token = _type_token(sql_type)
    if token in _INTEGER_TYPES:
        return "integer"
    if token in _NUMBER_TYPES:
        return "number"
    if token in _TEXT_TYPES:
        return "text"
    if token in _DATETIME_TYPES:
        return "datetime"
    if token in _BOOLEAN_TYPES:
        return "boolean"
    return token


def _types_compatible(left: str, right: str) -> bool:
    families = {_type_family(left), _type_family(right)}
    if len(families) == 1:
        return True
    return families <= {"integer", "number"}


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _table_exp(name: str, alias: str | None = None) -> exp.Expression:
    table = exp.Table(this=_ident(name))
    if alias is None:
        return table
    return exp.alias_(table, alias, table=True)


def _column_exp(name: str, table: str | None = None) -> exp.Column:
    return exp.Column(this=_ident(name), table=_ident(table) if table else None)


def _sql(expression: exp.Expression, dialect: str) -> str:
    return expression.sql(dialect=dialect)


def _source_stats_sql(dialect: str, source: RelationValidationEndpoint) -> str:
    column = _column_exp(source.column)
    return _sql(
        exp.Select()
        .select(
            exp.alias_(exp.Count(this=column), "source_non_null_count"),
            exp.alias_(
                exp.Count(this=exp.Distinct(expressions=[column.copy()])),
                "source_distinct_count",
            ),
        )
        .from_(_table_exp(source.table)),
        dialect,
    )


def _target_stats_sql(dialect: str, target: RelationValidationEndpoint) -> str:
    column = _column_exp(target.column)
    return _sql(
        exp.Select()
        .select(
            exp.alias_(exp.Count(this=column), "target_non_null_count"),
            exp.alias_(
                exp.Count(this=exp.Distinct(expressions=[column.copy()])),
                "target_distinct_count",
            ),
        )
        .from_(_table_exp(target.table)),
        dialect,
    )


def _target_duplicate_sql(dialect: str, target: RelationValidationEndpoint) -> str:
    grouped = (
        exp.Select()
        .select(_column_exp(target.column))
        .from_(_table_exp(target.table))
        .where(exp.Not(this=exp.Is(this=_column_exp(target.column), expression=exp.Null())))
        .group_by(_column_exp(target.column))
        .having(exp.GT(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(1)))
    )
    subquery = exp.Subquery(
        this=grouped, alias=exp.TableAlias(this=exp.to_identifier("duplicate_keys"))
    )
    return _sql(
        exp.Select()
        .select(exp.alias_(exp.Count(this=exp.Star()), "target_duplicate_count"))
        .from_(subquery),
        dialect,
    )


def _source_unmatched_sql(
    dialect: str,
    source: RelationValidationEndpoint,
    target: RelationValidationEndpoint,
) -> str:
    exists = exp.Exists(
        this=exp.Select()
        .select(exp.Literal.number(1))
        .from_(_table_exp(target.table, "t"))
        .where(
            exp.EQ(
                this=_column_exp(target.column, "t"),
                expression=_column_exp(source.column, "s"),
            )
        )
    )
    return _sql(
        exp.Select()
        .select(exp.alias_(exp.Count(this=exp.Star()), "source_unmatched_count"))
        .from_(_table_exp(source.table, "s"))
        .where(
            exp.And(
                this=exp.Not(
                    this=exp.Is(this=_column_exp(source.column, "s"), expression=exp.Null())
                ),
                expression=exp.Not(this=exists),
            )
        ),
        dialect,
    )


def _multiple_match_sql(
    dialect: str,
    source: RelationValidationEndpoint,
    target: RelationValidationEndpoint,
) -> str:
    source_keys = (
        exp.Select()
        .select(_column_exp(source.column).as_("join_key"))
        .from_(_table_exp(source.table))
        .where(exp.Not(this=exp.Is(this=_column_exp(source.column), expression=exp.Null())))
        .distinct()
    )
    grouped = (
        exp.Select()
        .select(exp.column("join_key", table="s"))
        .from_(
            exp.Subquery(
                this=source_keys,
                alias=exp.TableAlias(this=exp.to_identifier("s")),
            )
        )
        .join(
            _table_exp(target.table, "t"),
            on=exp.EQ(
                this=_column_exp(target.column, "t"),
                expression=exp.column("join_key", table="s"),
            ),
            join_type="INNER",
        )
        .group_by(exp.column("join_key", table="s"))
        .having(
            exp.GT(
                this=exp.Count(this=_column_exp(target.column, "t")),
                expression=exp.Literal.number(1),
            )
        )
    )
    subquery = exp.Subquery(
        this=grouped, alias=exp.TableAlias(this=exp.to_identifier("multiple_keys"))
    )
    return _sql(
        exp.Select()
        .select(exp.alias_(exp.Count(this=exp.Star()), "multiple_match_count"))
        .from_(subquery),
        dialect,
    )
