from __future__ import annotations

import sqlite3
from pathlib import Path
from time import monotonic

import pytest
from contracts.datalink import DataLinkConnectionGrantRead, DataLinkErrorCode
from contracts.datasources import MySqlConnectionConfig, SchemaSummaryRead
from contracts.status import DataSourceType
from pydantic import SecretStr

from server.connector import ConnectorError, create_connector, resolve_source_path
from server.connector.base import SourceColumn, SourceTable
from server.connector.csv import CsvConnector
from server.connector.mysql import MySqlConnector
from server.connector.sqlite import SqliteConnector

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_fixed_demo_sources_have_stable_schema() -> None:
    source_root = PROJECT_ROOT / "data"
    sqlite_path = resolve_source_path(source_root, "demo/ecommerce.sqlite", DataSourceType.SQLITE)
    csv_path = resolve_source_path(source_root, "demo/ecommerce_flat.csv", DataSourceType.CSV)

    sqlite_info = SqliteConnector(sqlite_path, "ds_demo", 1).inspect()
    csv_info = CsvConnector(csv_path, "ds_flat", 1).inspect()

    assert [table.name for table in sqlite_info.tables] == [
        "customers",
        "order_items",
        "orders",
        "payments",
        "products",
    ]
    assert [foreign_key.source_column for foreign_key in sqlite_info.tables[2].foreign_keys] == [
        "customer_id"
    ]
    assert [table.name for table in csv_info.tables] == ["dataset"]
    assert csv_info.tables[0].foreign_keys == []
    assert [column.name for column in csv_info.tables[0].columns][:4] == [
        "order_id",
        "customer_id",
        "customer_name",
        "email",
    ]


def test_source_path_rejects_unsafe_reference_without_leaking_root(tmp_path: Path) -> None:
    with pytest.raises(ConnectorError) as caught:
        resolve_source_path(tmp_path, "../outside.sqlite", DataSourceType.SQLITE)

    assert caught.value.code == DataLinkErrorCode.PATH_OUTSIDE_ROOT
    assert str(tmp_path) not in str(caught.value)


def test_source_path_rejects_symbolic_link(tmp_path: Path) -> None:
    outside_file = tmp_path.parent / "outside.csv"
    outside_file.write_text("id\n1\n", encoding="utf-8")
    linked_file = tmp_path / "linked.csv"
    try:
        linked_file.symlink_to(outside_file)
    except OSError as exc:
        pytest.skip(f"Cannot create symbolic link in this environment: {exc}")

    with pytest.raises(ConnectorError) as caught:
        resolve_source_path(tmp_path, "linked.csv", DataSourceType.CSV)

    assert caught.value.code == DataLinkErrorCode.PATH_OUTSIDE_ROOT


def test_source_path_rejects_type_mismatch(tmp_path: Path) -> None:
    csv_path = tmp_path / "source.csv"
    csv_path.write_text("id\n1\n", encoding="utf-8")

    with pytest.raises(ConnectorError) as caught:
        resolve_source_path(tmp_path, "source.csv", DataSourceType.SQLITE)

    assert caught.value.code == DataLinkErrorCode.BUILD_FAILED
    assert str(tmp_path) not in str(caught.value)


def test_sqlite_connector_uses_read_only_connection() -> None:
    source_path = resolve_source_path(
        PROJECT_ROOT / "data",
        "demo/ecommerce.sqlite",
        DataSourceType.SQLITE,
    )
    connector = SqliteConnector(source_path, "ds_demo", 1)

    with connector._connect() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE should_not_exist (id INTEGER)")


def test_connector_factory_returns_only_supported_types() -> None:
    source_root = PROJECT_ROOT / "data"
    csv_path = resolve_source_path(source_root, "demo/ecommerce_flat.csv", DataSourceType.CSV)
    sqlite_path = resolve_source_path(source_root, "demo/ecommerce.sqlite", DataSourceType.SQLITE)

    assert isinstance(create_connector(DataSourceType.CSV, csv_path, "ds_flat", 1), CsvConnector)
    assert isinstance(
        create_connector(DataSourceType.SQLITE, sqlite_path, "ds_demo", 1), SqliteConnector
    )


def _write_sqlite_with_status(tmp_path: Path) -> Path:
    path = tmp_path / "distinct.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE orders (status TEXT, amount INTEGER)")
    connection.executemany(
        "INSERT INTO orders VALUES (?, ?)",
        [("paid", 1), ("paid", 2), ("pending", 3), (None, 4)],
    )
    connection.commit()
    connection.close()
    return path


def test_sqlite_distinct_values_orders_by_frequency_and_honors_limit(tmp_path: Path) -> None:
    connector = SqliteConnector(_write_sqlite_with_status(tmp_path), "ds_distinct", 1)

    assert connector.distinct_values("orders", "status") == ("paid", "pending")
    assert connector.distinct_values("orders", "status", limit=1) == ("paid",)
    with pytest.raises(ConnectorError):
        connector.distinct_values("orders", "status", limit=0)
    with pytest.raises(ConnectorError):
        connector.distinct_values("missing_table", "status")


def test_csv_distinct_values_counts_whole_file_and_honors_limit(tmp_path: Path) -> None:
    path = tmp_path / "distinct.csv"
    path.write_text(
        "status,amount\npaid,1\npending,2\npaid,3\n,4\n",
        encoding="utf-8",
    )
    connector = CsvConnector(path, "ds_distinct", 1)

    assert connector.distinct_values("dataset", "status") == ("paid", "pending")
    assert connector.distinct_values("dataset", "status", limit=1) == ("paid",)
    with pytest.raises(ConnectorError):
        connector.distinct_values("dataset", "missing_column")
    with pytest.raises(ConnectorError):
        connector.distinct_values("other_table", "status")


def test_mysql_distinct_values_rejects_invalid_request_before_querying() -> None:
    """真实 MySQL 不进默认门禁；这里只覆盖请求守卫与死线检查。"""

    grant = DataLinkConnectionGrantRead(
        datasource_id="ds_mysql",
        schema_revision=1,
        connection_revision=1,
        config=MySqlConnectionConfig(host="db.internal", database="shop", username="reader"),
        password=SecretStr("pw"),
        schema=SchemaSummaryRead(datasource_id="ds_mysql", dialect="mysql", tables=[]),
    )
    connector = MySqlConnector(grant, deadline=monotonic() + 60)
    connector._tables = {
        "orders": SourceTable(
            name="orders",
            columns=[
                SourceColumn(name="status", dtype="varchar", nullable=True, is_primary_key=False)
            ],
            foreign_keys=[],
        )
    }

    with pytest.raises(ConnectorError):
        connector.distinct_values("orders", "missing_column")
    with pytest.raises(ConnectorError):
        connector.distinct_values("missing_table", "status")
    with pytest.raises(ConnectorError):
        connector.distinct_values("orders", "status", limit=0)
    connector.close()
