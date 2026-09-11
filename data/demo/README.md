# DataPilot Demo Data

阶段三的固定 Demo 由 `scripts/generate_demo_data.py` 生成。它使用固定随机种子
`20250101` 和 `2025-01-01` 至 `2025-01-31` 的时间窗口，因此每次生成的行、Schema
和外键都相同。

```powershell
uv run python scripts/generate_demo_data.py
```

生成文件：

- `ecommerce.sqlite`：`customers`、`products`、`orders`、`order_items`、`payments` 五张表。
  `orders.customer_id -> customers.customer_id`、`order_items.order_id -> orders.order_id`、
  `order_items.product_id -> products.product_id`、`payments.order_id -> orders.order_id` 都是
  SQLite 显式外键。
- `ecommerce_flat.csv`：同一批订单的单表版本，DataLink 固定把它视为 `dataset`，不生成跨表
  Join。

DataLink 手工演示时，将其受控根目录设为 `./data`，再使用相对引用
`demo/ecommerce.sqlite` 或 `demo/ecommerce_flat.csv`。`metric-definitions.yaml` 只供评测脚本
比较参考结果；DataLink 不读取它，也不会把其中口径写入图谱或幂等键。
