from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from contracts.datalink import DataLinkErrorCode
from contracts.status import DataSourceType

from server.connector.base import (
    MAX_SAMPLE_ROWS,
    ConnectorError,
    DatasourceInfo,
    SourceColumn,
    SourceForeignKey,
    SourceRow,
    SourceTable,
)


class SqliteConnector:
    """通过 SQLite 只读 URI 提取普通用户表、字段和显式外键。"""

    def __init__(self, source_path: Path, datasource_id: str, schema_revision: int) -> None:
        self.source_path = source_path
        self.datasource_id = datasource_id
        self.schema_revision = schema_revision

    def inspect(self) -> DatasourceInfo:
        """读取 SQLite 普通用户表，不把 View、Trigger 或系统表纳入图谱。"""

        try:
            with closing(self._connect()) as connection:
                table_names = self._table_names(connection)
                if not table_names:
                    raise ConnectorError(
                        DataLinkErrorCode.BUILD_FAILED, "SQLite has no user tables"
                    )
                tables = [self._read_table(connection, table_name) for table_name in table_names]
        except ConnectorError:
            raise
        except sqlite3.Error as exc:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "SQLite source cannot be read"
            ) from exc
        return DatasourceInfo(
            datasource_id=self.datasource_id,
            source_type=DataSourceType.SQLITE,
            schema_revision=self.schema_revision,
            tables=tables,
        )

    def sample_rows(self, table_name: str, limit: int = MAX_SAMPLE_ROWS) -> list[SourceRow]:
        """从白名单用户表读取有限样本，始终使用只读连接。"""

        if not 1 <= limit <= MAX_SAMPLE_ROWS:
            raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "Sample limit is invalid")
        try:
            with closing(self._connect()) as connection:
                if table_name not in self._table_names(connection):
                    raise ConnectorError(
                        DataLinkErrorCode.BUILD_FAILED, "SQLite table is unavailable"
                    )
                quoted_name = _quote_identifier(table_name)
                rows = connection.execute(
                    f"SELECT * FROM {quoted_name} LIMIT ?", (limit,)
                ).fetchall()
        except ConnectorError:
            raise
        except sqlite3.Error as exc:
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "SQLite source cannot be read"
            ) from exc
        return [_row_to_mapping(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        """以 `mode=ro` 打开 SQLite，阻断任何建表、写入或扩展加载。"""

        connection = sqlite3.connect(f"{self.source_path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        """Each SQLite operation already owns and closes its connection."""

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> list[str]:
        """只返回 SQLite 中非系统的真实普通表。"""

        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE ? ORDER BY name",
            ("table", "sqlite_%"),
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def _read_table(self, connection: sqlite3.Connection, table_name: str) -> SourceTable:
        """读取一张已由 SQLite 元数据列举出的表的结构事实。"""

        columns = [
            SourceColumn(
                name=str(row["name"]),
                dtype=str(row["type"] or "text").lower(),
                nullable=not bool(row["notnull"]),
                is_primary_key=bool(row["pk"]),
            )
            for row in connection.execute(
                'SELECT cid, name, type, "notnull", pk FROM pragma_table_info(?) ORDER BY cid',
                (table_name,),
            )
        ]
        if not columns:
            raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "SQLite table has no columns")
        foreign_keys = [
            SourceForeignKey(
                constraint_name=f"fk_{table_name}_{row['id']}",
                source_table=table_name,
                source_column=str(row["from"]),
                target_table=str(row["table"]),
                target_column=str(row["to"]),
            )
            for row in connection.execute(
                'SELECT id, seq, "table", "from", "to" '
                "FROM pragma_foreign_key_list(?) ORDER BY id, seq",
                (table_name,),
            )
        ]
        quoted_name = _quote_identifier(table_name)
        row_count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted_name}").fetchone()[0])
        return SourceTable(
            name=table_name,
            columns=columns,
            foreign_keys=foreign_keys,
            row_count=row_count,
        )


def _quote_identifier(name: str) -> str:
    """引用来自 SQLite 元数据的标识符，避免保留字或特殊字符破坏读取。"""

    escaped_name = name.replace('"', '""')
    return f'"{escaped_name}"'


def _row_to_mapping(row: sqlite3.Row) -> SourceRow:
    """将 SQLite 样本转换为后续画像可消费的有限标量值。"""

    return {
        key: value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
        for key, value in zip(row.keys(), row, strict=True)
    }
