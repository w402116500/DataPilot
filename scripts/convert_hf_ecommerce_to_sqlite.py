"""将 LaelaZ synthetic-ecommerce Parquet 样本转换为可分析的 SQLite。

运行方式：
    uv run --isolated --with pyarrow scripts/convert_hf_ecommerce_to_sqlite.py

已有目标文件时，只有显式传入 ``--replace`` 才会在完整校验通过后替换它。
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIRECTORY = PROJECT_ROOT / "data" / "external" / "laelaz-synthetic-ecommerce"
DEFAULT_OUTPUT_PATH = DEFAULT_SOURCE_DIRECTORY / "ecommerce.sqlite"


class TableSpec:
    """描述一个 Parquet 文件、SQLite 表和必须保留的主键。"""

    def __init__(self, *, table_name: str, columns: tuple[str, ...], primary_key: str) -> None:
        self.table_name = table_name
        self.columns = columns
        self.primary_key = primary_key


TABLE_SPECS = (
    TableSpec(
        table_name="customers",
        columns=("customer_id", "signup_date", "channel", "country"),
        primary_key="customer_id",
    ),
    TableSpec(
        table_name="products",
        columns=("product_id", "product_name", "category", "unit_price", "unit_cost"),
        primary_key="product_id",
    ),
    TableSpec(
        table_name="orders",
        columns=("order_id", "customer_id", "order_ts", "status"),
        primary_key="order_id",
    ),
    TableSpec(
        table_name="order_items",
        columns=("order_item_id", "order_id", "product_id", "quantity", "unit_price"),
        primary_key="order_item_id",
    ),
    TableSpec(
        table_name="events",
        columns=("event_id", "session_id", "customer_id", "event_type", "event_ts"),
        primary_key="event_id",
    ),
)

EXPECTED_ROW_COUNTS = {
    "customers": 2_000,
    "products": 120,
    "orders": 12_000,
    "order_items": 30_120,
    "events": 59_599,
}

SCHEMA_SQL = """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    signup_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    country TEXT NOT NULL
);

CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL,
    unit_cost REAL NOT NULL
);

CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_ts TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_ts TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_orders_order_ts ON orders(order_ts);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_order_items_product_id ON order_items(product_id);
CREATE INDEX idx_events_customer_id ON events(customer_id);
CREATE INDEX idx_events_event_ts ON events(event_ts);
CREATE INDEX idx_events_event_type ON events(event_type);
"""


def _parquet_file(source_directory: Path, table_name: str) -> Path:
    path = source_directory / f"{table_name}.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 Parquet 文件：{path}")
    return path


def _load_parquet_module():
    """延迟导入可选工具依赖，使普通项目检查不依赖 PyArrow。"""

    try:
        import pyarrow.parquet as parquet
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 pyarrow；请使用 `uv run --isolated --with pyarrow "
            "scripts/convert_hf_ecommerce_to_sqlite.py` 运行。"
        ) from exc
    return parquet


def _iter_rows(
    source_directory: Path,
    spec: TableSpec,
    *,
    batch_size: int = 1_000,
) -> Iterator[tuple[object, ...]]:
    parquet = _load_parquet_module()
    parquet_file = parquet.ParquetFile(_parquet_file(source_directory, spec.table_name))
    actual_columns = tuple(parquet_file.schema_arrow.names)
    if actual_columns != spec.columns:
        raise ValueError(
            f"{spec.table_name}.parquet 字段不符合预期："
            f"期望 {spec.columns!r}，实际 {actual_columns!r}"
        )

    for batch in parquet_file.iter_batches(batch_size=batch_size, columns=list(spec.columns)):
        for row in batch.to_pylist():
            yield tuple(_sqlite_value(row[column]) for column in spec.columns)


def _sqlite_value(value: object) -> object:
    """把 Parquet 的日期时间值稳定存成 SQLite 可排序的 ISO 文本。"""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bytes)) or value is None:
        return value
    raise TypeError(f"不支持写入 SQLite 的值类型：{type(value).__name__}")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_SQL)


def _insert_table(
    connection: sqlite3.Connection,
    source_directory: Path,
    spec: TableSpec,
) -> int:
    placeholders = ", ".join("?" for _ in spec.columns)
    columns = ", ".join(spec.columns)
    statement = f"INSERT INTO {spec.table_name} ({columns}) VALUES ({placeholders})"
    row_count = 0
    rows: list[tuple[object, ...]] = []
    for row in _iter_rows(source_directory, spec):
        rows.append(row)
        if len(rows) == 1_000:
            connection.executemany(statement, rows)
            row_count += len(rows)
            rows.clear()
    if rows:
        connection.executemany(statement, rows)
        row_count += len(rows)
    return row_count


def _single_value(connection: sqlite3.Connection, statement: str) -> object:
    row = connection.execute(statement).fetchone()
    if row is None:
        raise RuntimeError("SQLite 校验查询没有返回结果")
    return row[0]


def _validate_database(
    connection: sqlite3.Connection,
    expected_counts: Mapping[str, int],
) -> None:
    integrity_result = _single_value(connection, "PRAGMA integrity_check")
    if integrity_result != "ok":
        raise RuntimeError(f"SQLite integrity_check 失败：{integrity_result}")

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"SQLite 外键校验失败：{foreign_key_errors!r}")

    for spec in TABLE_SPECS:
        actual_count = _single_value(
            connection,
            f"SELECT COUNT(*) FROM {spec.table_name}",
        )
        if actual_count != expected_counts[spec.table_name]:
            raise RuntimeError(
                f"{spec.table_name} 行数不一致："
                f"期望 {expected_counts[spec.table_name]}，实际 {actual_count}"
            )

        unique_count = _single_value(
            connection,
            f"SELECT COUNT(DISTINCT {spec.primary_key}) FROM {spec.table_name}",
        )
        if unique_count != actual_count:
            raise RuntimeError(f"{spec.table_name}.{spec.primary_key} 存在重复值")


def convert(
    *,
    source_directory: Path = DEFAULT_SOURCE_DIRECTORY,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    replace: bool = False,
) -> dict[str, int]:
    """从固定电商 Parquet 数据集生成并校验一个 SQLite 文件。"""

    source_directory = source_directory.resolve()
    output_path = output_path.resolve()
    if output_path.exists() and not replace:
        raise FileExistsError(f"目标文件已存在，请传入 --replace 才会替换：{output_path}")
    if output_path.exists() and not output_path.is_file():
        raise ValueError(f"目标路径不是文件：{output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    expected_counts: dict[str, int] = {}
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            _create_schema(connection)
            for spec in TABLE_SPECS:
                expected_counts[spec.table_name] = _insert_table(
                    connection,
                    source_directory,
                    spec,
                )
                if expected_counts[spec.table_name] != EXPECTED_ROW_COUNTS[spec.table_name]:
                    raise RuntimeError(
                        f"{spec.table_name}.parquet 行数不符合已选样本版本："
                        f"期望 {EXPECTED_ROW_COUNTS[spec.table_name]}，"
                        f"实际 {expected_counts[spec.table_name]}"
                    )
            _validate_database(connection, EXPECTED_ROW_COUNTS)
            connection.commit()
        finally:
            # Windows 在连接关闭前不能替换 SQLite 文件。
            connection.close()
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return expected_counts


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将电商 Parquet 样本转换为 SQLite")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIRECTORY,
        help="Parquet 样本目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="生成的 SQLite 路径",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="完整校验成功后替换已有目标文件",
    )
    return parser.parse_args(argv)


def main() -> None:
    """解析命令行参数并输出可人工核对的生成摘要。"""

    args = _parse_args()
    counts = convert(
        source_directory=args.source_dir,
        output_path=args.output,
        replace=args.replace,
    )
    print(f"已生成 SQLite：{args.output}")
    for spec in TABLE_SPECS:
        print(f"- {spec.table_name}: {counts[spec.table_name]} 行")
    print("完整性、主键唯一性和外键校验均已通过。")


if __name__ == "__main__":
    main()
