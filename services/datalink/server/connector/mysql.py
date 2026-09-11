"""Bounded MySQL metadata and sampling behind an authorized connection grant."""

from __future__ import annotations

import math
import ssl
from time import monotonic

from contracts.datalink import DataLinkConnectionGrantRead, DataLinkErrorCode
from contracts.datasources import (
    ForeignKeyRead,
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
)
from contracts.status import DataSourceType
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from server.connector.base import (
    MAX_SAMPLE_ROWS,
    ConnectorError,
    DatasourceInfo,
    SourceColumn,
    SourceForeignKey,
    SourceRow,
    SourceTable,
)


class MySqlConnector:
    """One Build owns one disposable engine; neither credentials nor URL are persisted."""

    def __init__(self, grant: DataLinkConnectionGrantRead, deadline: float) -> None:
        self._grant = grant
        self._deadline = deadline
        self._tables: dict[str, SourceTable] = {}
        config = grant.config
        args = {
            "connect_timeout": config.connect_timeout_seconds,
            "read_timeout": 15,
            "write_timeout": 15,
        }
        if config.tls:
            args["ssl"] = ssl.create_default_context()
        self._engine = create_engine(
            URL.create(
                "mysql+pymysql",
                username=config.username,
                password=grant.password.get_secret_value(),
                host=config.host,
                port=config.port,
                database=config.database,
            ),
            connect_args=args,
            poolclass=NullPool,
            hide_parameters=True,
        )

    def _check_deadline(self) -> None:
        if monotonic() >= self._deadline:
            raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "MySQL Build deadline exceeded")

    def inspect(self) -> DatasourceInfo:
        self._check_deadline()
        tables: list[SourceTable] = []
        schemas: list[SchemaTableRead] = []
        try:
            with self._engine.connect() as conn:
                conn.exec_driver_sql("SET SESSION MAX_EXECUTION_TIME=15000")
                conn.exec_driver_sql("START TRANSACTION READ ONLY")
                inspector = inspect(conn)
                names = sorted(inspector.get_table_names())
                if not 1 <= len(names) <= 200:
                    raise ConnectorError(
                        DataLinkErrorCode.BUILD_FAILED, "MySQL table budget exceeded"
                    )
                for name in names:
                    self._check_deadline()
                    reflected = inspector.get_columns(name)
                    if not 1 <= len(reflected) <= 500:
                        raise ConnectorError(
                            DataLinkErrorCode.BUILD_FAILED, "MySQL column budget exceeded"
                        )
                    primary = inspector.get_pk_constraint(name).get("constrained_columns") or []
                    columns = [
                        SourceColumn(
                            name=c["name"],
                            dtype=str(c["type"]).lower(),
                            nullable=c["nullable"],
                            is_primary_key=c["name"] in primary,
                        )
                        for c in reflected
                    ]
                    foreign_keys, schema_keys = [], []
                    for index, fk in enumerate(inspector.get_foreign_keys(name)):
                        # References into other databases are outside this datasource.
                        if fk.get("referred_schema") not in {None, self._grant.config.database}:
                            continue
                        source, target = fk["constrained_columns"], fk["referred_columns"]
                        schema_keys.append(
                            ForeignKeyRead(
                                columns=source,
                                referenced_table=fk["referred_table"],
                                referenced_columns=target,
                            )
                        )
                        for ordinal, (left, right) in enumerate(zip(source, target, strict=True)):
                            foreign_keys.append(
                                SourceForeignKey(
                                    constraint_name=fk.get("name") or f"fk_{index}",
                                    source_table=name,
                                    source_column=left,
                                    target_table=fk["referred_table"],
                                    target_column=right,
                                    ordinal=ordinal,
                                    column_count=len(source),
                                )
                            )
                    tables.append(
                        SourceTable(name=name, columns=columns, foreign_keys=foreign_keys)
                    )
                    schemas.append(
                        SchemaTableRead(
                            name=name,
                            columns=[
                                SchemaColumnRead(name=c.name, type=c.dtype, nullable=c.nullable)
                                for c in columns
                            ],
                            primary_key=primary,
                            foreign_keys=schema_keys,
                        )
                    )
        except SQLAlchemyError:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "MySQL metadata is unavailable"
            ) from None
        actual = SchemaSummaryRead(
            datasource_id=self._grant.datasource_id, dialect="mysql", tables=schemas
        )
        if _structure(actual) != _structure(self._grant.schema_summary):
            raise ConnectorError(
                DataLinkErrorCode.REVISION_INVALID,
                "MySQL Schema changed; check the datasource again",
            )
        self._tables = {table.name: table for table in tables}
        return DatasourceInfo(
            datasource_id=self._grant.datasource_id,
            source_type=DataSourceType.MYSQL,
            schema_revision=self._grant.schema_revision,
            connection_revision=self._grant.connection_revision,
            tables=tables,
        )

    def sample_rows(self, table_name: str, limit: int = MAX_SAMPLE_ROWS) -> list[SourceRow]:
        self._check_deadline()
        table = self._tables.get(table_name)
        if table is None or not 1 <= limit <= MAX_SAMPLE_ROWS:
            raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "MySQL sample request is invalid")
        quote = self._engine.dialect.identifier_preparer.quote
        # Truncate text/blob values server-side before the driver allocates their payloads.
        columns = ", ".join(
            f"LEFT({quote(c.name)}, 4096) AS {quote(c.name)}"
            if any(t in c.dtype for t in ("char", "text", "blob", "binary", "json"))
            else quote(c.name)
            for c in table.columns
        )
        rows, size = [], 0
        try:
            with self._engine.connect() as conn:
                conn.exec_driver_sql("SET SESSION MAX_EXECUTION_TIME=15000")
                conn.exec_driver_sql("START TRANSACTION READ ONLY")
                with conn.execution_options(stream_results=True).execute(
                    text(f"SELECT {columns} FROM {quote(table_name)} LIMIT :limit"),
                    {"limit": limit},
                ) as result:
                    for row in result.mappings():
                        self._check_deadline()
                        item = {key: _scalar(value) for key, value in row.items()}
                        size += sum(len(str(value).encode("utf-8")) for value in item.values())
                        if size > 4 * 1024 * 1024:
                            raise ConnectorError(
                                DataLinkErrorCode.BUILD_FAILED, "MySQL sample byte budget exceeded"
                            )
                        rows.append(item)
        except SQLAlchemyError:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "MySQL samples are unavailable"
            ) from None
        return rows

    def close(self) -> None:
        self._engine.dispose()
        self._tables.clear()
        self._grant = None


def _structure(schema: SchemaSummaryRead) -> list[dict]:
    tables = []
    for table in sorted(schema.tables, key=lambda t: t.name):
        value = table.model_dump(exclude={"row_count"})
        value["foreign_keys"] = sorted(value["foreign_keys"], key=lambda fk: str(fk))
        tables.append(value)
    return tables


def _scalar(value: object):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value[:4096].hex()
    return str(value)[:4096]
