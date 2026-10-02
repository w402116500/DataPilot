"""中文电商基准评分：数值容差 + 单元格匹配，输出逐题 verdict。

用法（在仓库根目录）：
    uv run python scripts/score_benchmark.py --lint                  # 题库结构一致性检查
    uv run python scripts/score_benchmark.py commit-single-r1        # 评一个轮次
    uv run python scripts/score_benchmark.py commit-single-r1 commit-single-r2
    uv run python scripts/score_benchmark.py --bench-dir .tmp/ecommerce-cn-benchmark \
        --output .tmp/replay.json .tmp/ecommerce-cn-benchmark/commit-single-r1

轮次参数是 ``--bench-dir`` 下的目录名，也可以直接给出轮次目录路径（用于对
其他位置的轮次落盘重放评分）。对每个轮次目录下的 ``q*/answer.txt`` 与
``expected-answers.json`` 的 rows 逐行比对；缺省打印逐题汇总并每轮写
``score.json``，给出 ``--output`` 时改为把全部轮次结果写到指定路径。

维护约定：题面是唯一真相（见 data/benchmark/ecommerce-cn/NOTES.md）。
``expected-answers.json`` 是评分 gold 的唯一权威文件；题面与 gold 冲突时修
gold，不改题面。评分算法（0.05% 相对容差 + gold 小数位舍入 + 字符串/月份
同义匹配 + verdict 分桶）是历史结论的可比性基线，改动前先评估重放可比性。
--lint 只做结构一致性检查；题面-vs-gold 的语义核查走人工流程并记录在题库
NOTES.md。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_BENCH_DIR = Path("data/benchmark/ecommerce-cn")
TOL = 0.0005  # 0.05% 相对容差


def load_questions(path: Path) -> list[dict]:
    questions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            questions.append(json.loads(line))
    return questions


def load_expected(bench_dir: Path) -> dict[str, dict]:
    data = json.loads((bench_dir / "expected-answers.json").read_text(encoding="utf-8"))
    return {item["id"]: item for item in data}


def lint_bench(bench_dir: Path) -> list[str]:
    """结构一致性检查，返回 finding 列表（空列表即通过）。"""

    findings: list[str] = []
    question_ids: set[str] = set()
    questions_by_file: list[tuple[str, list[dict]]] = []
    for name in ("questions.jsonl", "questions-datalink.jsonl"):
        questions = load_questions(bench_dir / name)
        if not questions:
            findings.append(f"{name}: 没有解析到任何题目")
            continue
        questions_by_file.append((name, questions))
        ids = {question["id"] for question in questions}
        for question in questions:
            if not str(question.get("gold_sql") or "").strip():
                findings.append(f"{name}: {question['id']} 的 gold_sql 为空")
        if question_ids and ids != question_ids:
            findings.append(
                f"{name}: 与 questions.jsonl 的题 id 集合不一致，差异：{sorted(ids ^ question_ids)}"
            )
        question_ids |= ids
    expected = load_expected(bench_dir)
    expected_ids = set(expected)
    # 题面文件的 gold_sql 必须与评分 gold 同源（q13/q15 矛盾的防复发检查）：
    # expected-answers.json 是唯一权威，题面文件不得各留一份旧 gold。
    for name, questions in questions_by_file:
        for question in questions:
            expected_item = expected.get(question["id"])
            if expected_item is None:
                continue
            if question.get("gold_sql") != expected_item.get("gold_sql"):
                findings.append(
                    f"{name}: {question['id']} 的 gold_sql 与 expected-answers.json 不一致"
                )
    missing = question_ids - expected_ids
    if missing:
        findings.append(f"expected-answers.json: 缺少题面要求的 id：{sorted(missing)}")
    for qid, item in expected.items():
        rows = item.get("rows") or []
        if item.get("row_count") != len(rows):
            findings.append(
                f"expected-answers.json: {qid} 的 row_count={item.get('row_count')}"
                f" 与 len(rows)={len(rows)} 不一致"
            )
        columns = item.get("columns") or []
        for index, row in enumerate(rows):
            if len(row) != len(columns):
                findings.append(
                    f"expected-answers.json: {qid} 第 {index} 行长度 {len(row)}"
                    f" 与 columns 数 {len(columns)} 不一致"
                )
        if not str(item.get("gold_sql") or "").strip():
            findings.append(f"expected-answers.json: {qid} 的 gold_sql 为空")
    extras = expected_ids - question_ids
    if extras:
        findings.append(f"expected-answers.json: 存在题面没有的 id：{sorted(extras)}")
    return findings


def num_match(text: str, value) -> bool:
    """答案文本中是否存在与 value 匹配的数字：舍入感知或 0.05% 相对容差。

    真值常为四舍五入结果（如 4.2 vs 实际 4.1962），按 gold 的小数位数
    接受任何舍入后等于 gold 的数值；另保留相对容差兜底。
    """
    gold = float(value)
    s = repr(value) if not isinstance(value, str) else value
    if "." in s:
        decimals = len(s.rstrip("0").split(".")[1]) if "." in s else 0
    else:
        decimals = 0
    target = abs(gold)
    lo, hi = target * (1 - TOL), target * (1 + TOL)
    for m in re.finditer(r"-?\d[\d,]*\.?\d*", text):
        token = m.group().replace(",", "")
        try:
            x = float(token)
        except ValueError:
            continue
        if lo <= abs(x) <= hi:
            return True
        if decimals and abs(round(x, decimals) - gold) < 1e-9:
            return True
    return False


_DATE_CELL = re.compile(r"^(\d{4})[-/](\d{1,2})$")


def _date_label_match(text: str, cell: str) -> bool:
    """2025-01 ↔ “1月”/“01月” 等记法差异按同义处理。"""
    m = _DATE_CELL.match(cell.strip())
    if not m:
        return False
    month = int(m.group(2))
    return re.search(rf"{month}\s*月", text) or re.search(rf"{month:02d}\s*月", text)


def cell_match(text: str, cell) -> bool:
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return num_match(text, float(cell))
    s = str(cell).strip()
    if not s:
        return True
    return s in text or _date_label_match(text, s)


def score_answer(answer: str, expected: dict) -> tuple[str, str]:
    """返回 (verdict, detail)。"""
    rows = expected.get("rows") or []
    if not rows:
        return "no-gold", "expected-answers 无 rows"
    matched = 0
    misses: list[str] = []
    for row in rows:
        if all(cell_match(answer, cell) for cell in row):
            matched += 1
        else:
            misses.append(" | ".join(str(c) for c in row))
    if matched == len(rows):
        return "correct", f"{matched}/{len(rows)} rows matched"
    if matched == 0:
        return "no-answer", "0 行命中"
    return "partial", f"{matched}/{len(rows)} 行命中，缺: {' ;; '.join(misses[:3])}"


def resolve_round_dir(bench_dir: Path, round_name: str) -> Path | None:
    named = bench_dir / round_name
    if named.is_dir():
        return named
    as_path = Path(round_name)
    return as_path if as_path.is_dir() else None


def score_round(round_dir: Path, expected: dict[str, dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for qdir in sorted(round_dir.glob("q*")):
        if not qdir.is_dir():
            continue
        qid = qdir.name
        meta = json.loads((qdir / "run-meta.json").read_text(encoding="utf-8"))
        answer_file = qdir / "answer.txt"
        answer = answer_file.read_text(encoding="utf-8") if answer_file.exists() else ""
        if meta.get("error_code") or meta.get("status") != "succeeded":
            verdict, detail = "run-failed", str(meta.get("error_code") or meta.get("status"))
        elif meta.get("completion_kind") != "completed":
            verdict, detail = "no-answer", f"partial/{meta.get('incomplete_reason')}"
        else:
            verdict, detail = score_answer(answer, expected.get(qid, {}))
        results[qid] = {
            "verdict": verdict,
            "detail": detail,
            "completed": meta.get("completion_kind"),
            "answer_chars": len(answer),
        }
    return results


def print_round(round_name: str, results: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    total = len(results)
    all_correct = counts.get("correct", 0)
    print(f"\n===== {round_name}: 全对 {all_correct}/{total} | {counts} =====")
    for qid, r in results.items():
        if r["verdict"] != "correct":
            print(f"  {qid}: {r['verdict']}  {r['detail'][:150]}")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="中文电商基准评分")
    parser.add_argument(
        "--bench-dir",
        type=Path,
        default=DEFAULT_BENCH_DIR,
        help="题库目录（含 expected-answers.json）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="结果 JSON 输出路径；缺省每轮写 score.json 到轮次目录",
    )
    parser.add_argument("--lint", action="store_true", help="只做题库结构一致性检查")
    parser.add_argument("rounds", nargs="*", help="轮次目录名（--bench-dir 下）或轮次目录路径")
    args = parser.parse_args()

    if args.lint:
        findings = lint_bench(args.bench_dir)
        if findings:
            for finding in findings:
                print(f"[lint] {finding}")
            sys.exit(1)
        print(f"[lint] {args.bench_dir} 结构一致性检查通过")
        return

    if not args.rounds:
        parser.error("至少提供一个轮次目录，或使用 --lint")
    expected = load_expected(args.bench_dir)
    collected: dict[str, dict] = {}
    for round_name in args.rounds:
        round_dir = resolve_round_dir(args.bench_dir, round_name)
        if round_dir is None:
            print(f"[{round_name}] 目录不存在，跳过")
            continue
        results = score_round(round_dir, expected)
        counts = print_round(round_name, results)
        collected[round_name] = {"counts": counts, "results": results}
        if args.output is None:
            (round_dir / "score.json").write_text(
                json.dumps(collected[round_name], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(collected, ensure_ascii=False, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
