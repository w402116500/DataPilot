from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from contracts.datalink import DataLinkErrorCode
from contracts.status import DataSourceType

from server.connector import ConnectorError, create_connector, resolve_source_path
from server.connector.csv import CsvConnector
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
