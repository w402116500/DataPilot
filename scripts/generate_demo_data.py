"""生成阶段三固定电商 Demo 的 SQLite 与 CSV 文件。"""

from __future__ import annotations

import csv
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DEMO_SEED = 20250101
DEMO_START_DATE = date(2025, 1, 1)
DEMO_END_DATE = date(2025, 1, 31)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIRECTORY = PROJECT_ROOT / "data" / "demo"
SQLITE_PATH = DEMO_DIRECTORY / "ecommerce.sqlite"
CSV_PATH = DEMO_DIRECTORY / "ecommerce_flat.csv"

CUSTOMERS = [
    (1, "Ada Lin", "ada.lin@example.test", "Shanghai", "2024-09-08"),
    (2, "Bruno Zhao", "bruno.zhao@example.test", "Beijing", "2024-10-12"),
    (3, "Chen Wu", "chen.wu@example.test", "Shenzhen", "2024-11-20"),
    (4, "Diana Sun", "diana.sun@example.test", "Hangzhou", "2024-12-02"),
    (5, "Evan Gu", "evan.gu@example.test", "Chengdu", "2024-12-15"),
    (6, "Faye He", "faye.he@example.test", "Wuhan", "2024-12-28"),
]

PRODUCTS = [
    (1, "Wireless Earbuds", "electronics", 199.0),
    (2, "Travel Mug", "home", 89.0),
    (3, "Notebook Set", "stationery", 39.0),
    (4, "Desk Lamp", "home", 159.0),
    (5, "Phone Stand", "electronics", 49.0),
]


def _build_orders() -> tuple[
    list[tuple[object, ...]], list[tuple[object, ...]], list[tuple[object, ...]]
]:
    """用固定种子生成订单、订单项和支付记录，保证每次输出相同。"""

    generator = random.Random(DEMO_SEED)
    statuses = ("paid", "completed", "failed", "pending", "canceled", "refunded")
    channels = ("web", "mobile", "store")
    payment_methods = ("card", "wallet", "bank_transfer")
    products_by_id = {product[0]: product for product in PRODUCTS}
    orders: list[tuple[object, ...]] = []
    order_items: list[tuple[object, ...]] = []
    payments: list[tuple[object, ...]] = []
    item_id = 1

    for index in range(24):
        order_id = 1001 + index
        customer_id = 1 + ((index * 3) % len(CUSTOMERS))
        status = statuses[index % len(statuses)]
        order_date = DEMO_START_DATE + timedelta(days=generator.randrange(31))
        selected_product_ids = generator.sample(list(products_by_id), k=1 + (index % 2))
        item_rows: list[tuple[object, ...]] = []
        total_amount = 0.0
        for product_id in selected_product_ids:
            product = products_by_id[product_id]
            quantity = 1 + generator.randrange(3)
            unit_price = float(product[3])
            total_amount += quantity * unit_price
            item_rows.append((item_id, order_id, product_id, quantity, unit_price))
            item_id += 1

        rounded_total = round(total_amount, 2)
        orders.append(
            (
                order_id,
                customer_id,
                order_date.isoformat(),
                status,
                channels[index % len(channels)],
                rounded_total,
            )
        )
        order_items.extend(item_rows)
        payment_status = {
            "paid": "succeeded",
            "completed": "succeeded",
            "refunded": "succeeded",
            "failed": "failed",
            "pending": "pending",
            "canceled": "failed",
        }[status]
        payments.append(
            (
                5001 + index,
                order_id,
                payment_methods[index % len(payment_methods)],
                payment_status,
                rounded_total,
            )
        )

    return orders, order_items, payments


def _write_sqlite(
    orders: list[tuple[object, ...]],
    order_items: list[tuple[object, ...]],
    payments: list[tuple[object, ...]],
) -> None:
    """覆盖固定 Demo SQLite，写入显式主键和外键关系。"""

    SQLITE_PATH.unlink(missing_ok=True)
    with sqlite3.connect(SQLITE_PATH) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE customers (
                customer_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                city TEXT NOT NULL,
                registered_at TEXT NOT NULL
            );
            CREATE TABLE products (
                product_id INTEGER PRIMARY KEY,
                product_name TEXT NOT NULL,
                category TEXT NOT NULL,
                unit_price REAL NOT NULL
            );
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                order_date TEXT NOT NULL,
                status TEXT NOT NULL,
                channel TEXT NOT NULL,
                total_amount REAL NOT NULL,
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
            CREATE TABLE payments (
                payment_id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                payment_method TEXT NOT NULL,
                payment_status TEXT NOT NULL,
                paid_amount REAL NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );
            """
        )
        connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", CUSTOMERS)
        connection.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", PRODUCTS)
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", orders)
        connection.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?, ?, ?)",
            order_items,
        )
        connection.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?)", payments)


def _write_csv(orders: list[tuple[object, ...]], payments: list[tuple[object, ...]]) -> None:
    """把订单和客户信息展平为一个固定的 CSV `dataset`。"""

    customers_by_id = {customer[0]: customer for customer in CUSTOMERS}
    payments_by_order = {payment[1]: payment for payment in payments}
    fieldnames = [
        "order_id",
        "customer_id",
        "customer_name",
        "email",
        "city",
        "order_date",
        "status",
        "channel",
        "total_amount",
        "payment_method",
        "payment_status",
    ]
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for order in orders:
            customer = customers_by_id[int(order[1])]
            payment = payments_by_order[int(order[0])]
            writer.writerow(
                {
                    "order_id": order[0],
                    "customer_id": order[1],
                    "customer_name": customer[1],
                    "email": customer[2],
                    "city": customer[3],
                    "order_date": order[2],
                    "status": order[3],
                    "channel": order[4],
                    "total_amount": f"{float(order[5]):.2f}",
                    "payment_method": payment[2],
                    "payment_status": payment[3],
                }
            )


def main() -> None:
    """生成两个固定文件，并输出可人工核对的相对路径。"""

    if DEMO_END_DATE < DEMO_START_DATE:
        raise RuntimeError("Demo date window is invalid")
    DEMO_DIRECTORY.mkdir(parents=True, exist_ok=True)
    orders, order_items, payments = _build_orders()
    _write_sqlite(orders, order_items, payments)
    _write_csv(orders, payments)
    print("Generated data/demo/ecommerce.sqlite and data/demo/ecommerce_flat.csv")


if __name__ == "__main__":
    main()
