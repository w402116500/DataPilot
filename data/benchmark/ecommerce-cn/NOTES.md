# 题库核查记录（ecommerce-cn 基准）

维护约定：**题面是唯一真相**。`gold_sql`/`columns`/`rows` 必须能由题面约束推导；
冲突时修 gold，不改题面。`expected-answers.json` 是评分 gold 的唯一权威文件；
`questions.jsonl` 与 `questions-datalink.jsonl` 的**题面文本与元数据字段**是
gitignored 原件（`data/external/` 与 `.tmp/`）的迁移快照，不改题面；其 `gold_sql`
字段与 expected-answers.json 保持同源（q13/q15 于 2026-09-30 随 gold 修正同步
更新，见修正明细），避免题面文件与评分 gold 各说各话。评分与重放一律以
`expected-answers.json` 为准。

结构一致性（两份题面 id 一致、每题有 gold、row_count==len(rows)、columns 与行
长度一致、两份题面 gold_sql 与 expected-answers.json 逐题同源）由
`scripts/score_benchmark.py --lint` 机械核查；题面 vs gold 的语义
核查靠人工逐题比对并记录于本文件。

## 逐题核查结论（2026-09-30，20/20 题）

| id | 结论 |
| --- | --- |
| q01 | 一致：题面问顾客总数，gold `COUNT(*)` 单行 2400。 |
| q02 | 一致：题面问类目数并列出名称，gold 枚举 8 行，行数即类目数。 |
| q03 | 一致：支付方式 + 订单数，4 组。 |
| q04 | 一致：订单状态 + 订单数，5 组。 |
| q05 | 一致：大区 + 顾客数，8 组。 |
| q06 | 一致：题面"按售出件数排序"隐含件数列，gold 的 `total_qty` 是排序键而非超出题面的列。 |
| q07 | 一致：总销售额单行。 |
| q08 | 一致：已取消订单金额单行。 |
| q09 | 一致：类目销售额，8 组。 |
| q10 | 一致：大区销售额，8 组。 |
| q11 | 一致：大区平均订单金额，8 组。 |
| q12 | 一致：题面明确"给出城市和消费金额"，gold 两列 3 行。 |
| q13 | **修正**：题面只问店名与评分，gold 多了题面未要求的城市列，导致行匹配失败。去掉城市列（gold_sql/columns/rows 同步），见下。 |
| q14 | 一致：2026 年 1-9 月逐月销售额，9 组。 |
| q15 | **修正**：题面明确"只看 1-8 月"，旧 gold_sql 无月份约束且 rows 含题面排除的 2025-09..12。gold_sql 补 `AND MONTH(o.order_ts) BETWEEN 1 AND 8`，rows 删 4 行（数值不动），见下。 |
| q16 | 一致（记录不动）：对比题以两档各自的原始人均值为 gold，"高多少"由答案文本推导差值；notes 已说明人均口径。gold 未超出题面事实范围。 |
| q17 | 一致：各会员等级平均评分，4 组。 |
| q18 | 一致：题面问上海店铺，gold 只含 `shop_name` 一列 2 行。 |
| q19 | 一致：类目名/件数/占比三列对应题面两问，单行。 |
| q20 | 一致：2026-09 浏览未下单顾客数，单行 612。 |

## 修正明细

1. **q13**：`gold_sql` → `SELECT shop_name, rating FROM shops ORDER BY rating DESC, shop_id LIMIT 3`；
   `columns` → `['shop_name', 'rating']`；rows 三行各去掉城市（店名与 4.9 评分不动）；
   `row_count` 仍为 3。实际恰有 3 家 4.9 并列，`shop_id` 决胜无歧义，notes 保留。
2. **q15**：`gold_sql` 在 `WHERE o.order_ts < '2026-09-01'` 后补
   `AND MONTH(o.order_ts) BETWEEN 1 AND 8`（保持原 SQL 风格）；rows 删去
   2025-09/10/11/12 四行，剩 16 行（2025-01..08 与 2026-01..08，数值不动）；
   `row_count` → 16。16 行数值已被模型三轮运行（commit-single-r1/r2/r3）精确复现。
3. **gold_sql 同源（2026-09-30 复核补充）**：上述两题的 `gold_sql` 修正同步落到
   `questions.jsonl` 与 `questions-datalink.jsonl`（仅 gold_sql 字段，题面文本与
   其他字段不动），与 expected-answers.json 逐字一致；其余 18 题的 gold_sql
   本就与 expected-answers.json 一致。

## 存疑记录（只记录，不改动）

- q15 的 notes 文本仍写"20 组（2025-01..2025-08, 2026-01..2026-08）"——括号枚举
  实为 16 个月，"20 组"是与旧 gold 一致的旧账。按"题面文本不改、迁移快照不动"
  约定，questions*.jsonl 与 expected-answers.json 的 notes 均保留原文。
- q06 的 `total_qty` 为 192.0 等浮点形式（`SUM(quantity)` 结果），与题面无冲突，
  评分按数值匹配，不改。
