"""图谱 SQLite 的连接、初始化和短事务边界。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class GraphStorage:
    """为 DataLink 图谱提供独立 SQLite 连接和原子写入事务。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        """创建并升级图谱表；Schema 只属于 DataLink，不接入主后端 Migration。"""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path = Path(__file__).with_name("schema.sql")
        with self.connection() as connection:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(graph_builds)").fetchall()
            }
            if "rebuild_key" not in columns:
                connection.execute("ALTER TABLE graph_builds ADD COLUMN rebuild_key TEXT")
                connection.execute(
                    "UPDATE graph_builds SET rebuild_key = id WHERE rebuild_key IS NULL"
                )
            if "connection_revision" not in columns:
                connection.execute(
                    "ALTER TABLE graph_builds ADD COLUMN connection_revision INTEGER NOT NULL "
                    "DEFAULT 0 CHECK(connection_revision >= 0)"
                )
            if "origin_kind" not in columns:
                connection.execute(
                    "ALTER TABLE graph_builds ADD COLUMN origin_kind TEXT NOT NULL "
                    "DEFAULT 'automated'"
                )
            if "publication_state" not in columns:
                connection.execute(
                    "ALTER TABLE graph_builds ADD COLUMN publication_state TEXT NOT NULL "
                    "DEFAULT 'published'"
                )
            if "base_graph_version" not in columns:
                connection.execute("ALTER TABLE graph_builds ADD COLUMN base_graph_version TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_builds_datasource_rebuild_key "
                "ON graph_builds (datasource_id, rebuild_key)"
            )
            edge_columns = connection.execute("PRAGMA table_info(edges)").fetchall()
            if any(row["name"] == "confidence" and row["notnull"] for row in edge_columns):
                # SQLite cannot relax NOT NULL in place. Copy within one transaction;
                # no table references edges, so inbound foreign keys are unaffected.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("""CREATE TABLE edges_nullable (
                        id TEXT NOT NULL, datasource_id TEXT NOT NULL, build_id TEXT NOT NULL,
                        graph_version TEXT NOT NULL, source_id TEXT NOT NULL,
                        target_id TEXT NOT NULL, type TEXT NOT NULL,
                        confidence REAL CHECK(confidence >= 0 AND confidence <= 1),
                        evidence_json TEXT, properties_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY(build_id,id), UNIQUE(build_id,source_id,target_id,type),
                        FOREIGN KEY(build_id) REFERENCES graph_builds(id) ON DELETE CASCADE,
                        FOREIGN KEY(build_id,source_id) REFERENCES nodes(build_id,id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY(build_id,target_id) REFERENCES nodes(build_id,id)
                            ON DELETE RESTRICT
                    )""")
                    connection.execute("INSERT INTO edges_nullable SELECT * FROM edges")
                    connection.execute("DROP TABLE edges")
                    connection.execute("ALTER TABLE edges_nullable RENAME TO edges")
                    connection.execute(
                        "CREATE INDEX ix_edges_version_type ON edges "
                        "(datasource_id, graph_version, type)"
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise

    def check(self) -> bool:
        """确认图谱数据库可读写，但不把底层异常或路径暴露给调用方。"""

        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            if self.database_path.exists() and self.database_path.is_dir():
                return False
            with self.connection() as connection:
                connection.execute("SELECT 1")
        except (OSError, sqlite3.Error):
            return False
        return True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """打开短生命周期连接，确保所有连接都启用外键和 WAL。"""

        connection = sqlite3.connect(self.database_path, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """使用短暂的写事务串行化 Build 与 Graph Head 的状态变更。"""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
