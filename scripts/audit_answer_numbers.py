"""答案数字溯源审计：把答案正文里的数字与已验证的查询事实/查询结果逐个对照。

用法（仓库根目录）：
    uv run python scripts/audit_answer_numbers.py --bench-dir .tmp/ecommerce-cn-benchmark \
        commit-single-r1 commit-single-r2 commit-single-r3

对每题：从 events.jsonl 的 answer.ready.claim_audit_summary_json 收集通过校验的 Claim 数值，
从 tool-calls.json 的 succeeded output_summary 收集查询结果行值，组成"可溯源值集合"；
再从 answer.txt 抽出全部数字，逐个归一后对照。输出总数、命中数与未命中清单（含上下文）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

NUM_RE = re.compile(r"\d[\d,，]*(?:\.\d+)?")


def norm(value) -> str | None:
    try:
        return str(Decimal(str(value).replace(",", "").replace("，", "")).normalize())
    except (InvalidOperation, ValueError):
        return None


def extract_answer_numbers(text: str) -> list[tuple[str, str, str]]:
    """返回 (上下文, 归一值, 类别)。先剔除引用标记与列表序号，避免把非数据数字算进来。"""
    cleaned = re.sub(r"\[\d{1,3}\]", "", text)  # 引用标记 [1] [2][3]
    cleaned = re.sub(r"(?m)^(\s*)(\d{1,2})([\.、])\s+", r"\1", cleaned)  # 有序列表序号
    cleaned = re.sub(r"\|\s*\d{1,4}\s*\|", "||", cleaned)  # 表格行号列
    cleaned = re.sub(r"\b\d{1,4}\s*/\s*\d{1,4}\b", " ", cleaned)  # 准入行数 N/N
    cleaned = re.sub(r"(?:var)?char\(\d+(?:,\s*\d+)?\)", " ", cleaned, flags=re.I)  # 类型名
    cleaned = re.sub(r"decimal\(\d+(?:,\s*\d+)?\)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"材料\s*\d+", "材料", cleaned)  # 材料编号
    out = []
    for m in NUM_RE.finditer(cleaned):
        raw = m.group(0)
        start = max(0, m.start() - 8)
        end = min(len(cleaned), m.end() + 8)
        ctx = cleaned[start:end].replace("\n", " ")
        n = norm(raw)
        if n is None:
            continue
        after = cleaned[m.end() : m.end() + 2]
        kind = "other"
        if n in {"2025", "2026", "2024", "2027"}:
            kind = "year"
        elif after.startswith("月") or "月" in cleaned[m.end() : m.end() + 3]:
            kind = "month"
        out.append((ctx, n, kind))
    return out


def collect_verified_values(qdir: Path) -> tuple[set[str], list[str]]:
    """可溯源值集合 = 通过校验的 Claim facts 数值 + 成功 SQL 查询结果行值。"""
    verified: set[str] = set()
    sources: list[str] = []

    events = []
    ev_path = qdir / "events.jsonl"
    if ev_path.exists():
        for line in ev_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    for e in events:
        if e.get("type") != "answer.ready":
            continue
        raw = (e.get("payload") or {}).get("claim_audit_summary_json")
        if not raw:
            continue
        claims = json.loads(raw) if isinstance(raw, str) else raw
        for claim in claims:
            if claim.get("commit_status") != "passed":
                continue
            for fact in claim.get("facts") or []:
                n = norm(fact.get("value"))
                if n is not None:
                    verified.add(n)
                    sources.append(f"claim:{fact.get('name')}={fact.get('value')}")

    tc_path = qdir / "tool-calls.json"
    if tc_path.exists():
        try:
            calls = json.loads(tc_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            calls = []
        for call in calls if isinstance(calls, list) else []:
            if call.get("status") != "succeeded":
                continue
            summary = call.get("output_summary") or {}
            rc = summary.get("row_count")
            n_rc = norm(rc)
            if n_rc is not None:
                verified.add(n_rc)
            rows = summary.get("rows") or []
            for row in rows if isinstance(rows, list) else []:
                for cell in row if isinstance(row, list) else []:
                    n = norm(cell)
                    if n is not None:
                        verified.add(n)

    return verified, sources


def audit_round(round_dir: Path) -> dict:
    per_q = []
    for qdir in sorted(p for p in round_dir.iterdir() if p.is_dir() and p.name.startswith("q")):
        answer_path = qdir / "answer.txt"
        if not answer_path.exists():
            continue
        text = answer_path.read_text(encoding="utf-8")
        verified, sources = collect_verified_values(qdir)
        numbers = extract_answer_numbers(text)
        matched = [(ctx, n) for ctx, n, _ in numbers if n in verified]
        unmatched = [(ctx, n, kind) for ctx, n, kind in numbers if n not in verified]
        per_q.append(
            {
                "qid": qdir.name,
                "answer_chars": len(text),
                "verified_value_count": len(verified),
                "numbers_total": len(numbers),
                "numbers_matched": len(matched),
                "unmatched": [(c, n, k) for c, n, k in unmatched[:12]],
            }
        )
    total = sum(q["numbers_total"] for q in per_q)
    hit = sum(q["numbers_matched"] for q in per_q)
    return {
        "round": str(round_dir),
        "questions": len(per_q),
        "numbers_total": total,
        "numbers_matched": hit,
        "match_rate": round(hit / total, 4) if total else None,
        "unmatched_samples": [
            {"qid": q["qid"], "ctx": ctx, "value": n, "kind": k}
            for q in per_q
            for ctx, n, k in q["unmatched"][:4]
        ][:24],
        "per_question": per_q,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="答案数字溯源审计")
    parser.add_argument("rounds", nargs="+", help="轮次目录名或路径")
    parser.add_argument("--bench-dir", type=Path, default=Path(".tmp/ecommerce-cn-benchmark"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    results = []
    for name in args.rounds:
        p = Path(name)
        round_dir = p if p.exists() and p.is_dir() else args.bench_dir / name
        results.append(audit_round(round_dir))

    for r in results:
        print(
            f"== {Path(r['round']).name}: {r['questions']} 题 | 答案数字 {r['numbers_total']} 个 | "
            f"可溯源 {r['numbers_matched']} ({r['match_rate']}) =="
        )
        from collections import Counter
        kinds = Counter(u.get("kind", "?") for q in r["per_question"] for _, n, k in [] ) if False else None
        for u in r["unmatched_samples"][:8]:
            print(f"   未命中[{u.get('kind','?')}] {u['qid']}: …{u['ctx']}… (值 {u['value']})")

    grand_total = sum(r["numbers_total"] for r in results)
    grand_hit = sum(r["numbers_matched"] for r in results)
    print(
        f"\n合计: {grand_total} 个数字, 可溯源 {grand_hit} "
        f"({round(grand_hit / grand_total, 4) if grand_total else 'N/A'})"
    )
    if args.output:
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"明细已写入 {args.output}")


if __name__ == "__main__":
    main()
