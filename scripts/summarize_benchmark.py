"""离线汇总评测轮次目录：修复自愈率、上下文尺寸分布、预算达标率。

用法（仓库根目录）：
    uv run python scripts/summarize_benchmark.py commit-single-r1 commit-single-r2
    uv run python scripts/summarize_benchmark.py --bench-dir .tmp/ecommerce-cn-benchmark --all
    uv run python scripts/summarize_benchmark.py <任意轮次绝对路径> --output .tmp/rounds-summary.json

对每个轮次目录下的 qXX/events.jsonl、qXX/sql-audits.json、qXX/run-meta.json 做纯本地统计，
不调用任何模型。输出：每轮的修复统计（Opening 修复次数/涉及 Run 数、SQL 修复链与成功数）、
每回合上下文尺寸分布（token/字符，min/中位/max）、预算达标率、压缩与重试计数。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

DEFAULT_BENCH_DIR = Path(".tmp/ecommerce-cn-benchmark")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def question_dirs(round_dir: Path) -> list[Path]:
    return sorted(p for p in round_dir.iterdir() if p.is_dir() and p.name.startswith("q") and (p / "events.jsonl").exists())


def summarize_question(qdir: Path) -> dict | None:
    events = load_jsonl(qdir / "events.jsonl")
    if not events:
        return None
    meta = load_json(qdir / "run-meta.json") or {}

    opening_model_calls = 0
    opening_repair_calls = 0
    turn_tokens: list[int] = []
    turn_chars: list[int] = []
    over_budget = 0
    compactions = 0
    context_retries = 0
    ws_compacted = 0
    run_succeeded = None
    final_answer_seen = False

    for e in events:
        etype = e.get("type", "")
        payload = e.get("payload") or {}
        if etype == "run.preparation.completed":
            opening_model_calls += int(payload.get("opening_model_calls") or 0)
            opening_repair_calls += int(payload.get("opening_repair_calls") or 0)
        elif etype == "agent.turn.completed":
            tokens = payload.get("model_input_tokens")
            budget = payload.get("input_budget_tokens")
            if isinstance(tokens, (int, float)):
                turn_tokens.append(int(tokens))
                if isinstance(budget, (int, float)) and tokens > budget:
                    over_budget += 1
            chars = payload.get("model_input_chars")
            if isinstance(chars, (int, float)):
                turn_chars.append(int(chars))
            compactions += int(payload.get("context_compaction_count") or 0)
            context_retries += int(payload.get("context_retry_count") or 0)
            if payload.get("working_set_compacted"):
                ws_compacted += 1
        elif etype == "final_answer.request.started":
            final_answer_seen = True
        elif etype == "run.succeeded":
            run_succeeded = True
        elif etype == "run.failed":
            run_succeeded = False

    if run_succeeded is None:
        run_succeeded = bool(meta.get("status") == "succeeded")

    audits = load_json(qdir / "sql-audits.json") or []
    chains: dict[str, list[dict]] = {}
    for a in audits if isinstance(audits, list) else []:
        key = str(a.get("tool_call_id") or a.get("id") or len(chains))
        chains.setdefault(key, []).append(a)
    sql_repair_attempts = 0
    sql_repair_chains = 0
    sql_repair_success = 0
    for rows in chains.values():
        attempts = sorted(rows, key=lambda r: int(r.get("attempt_no") or 1))
        if len(attempts) > 1:
            sql_repair_chains += 1
            sql_repair_attempts += len(attempts) - 1
            final_status = (attempts[-1].get("status") or "").lower()
            if final_status == "succeeded":
                sql_repair_success += 1

    return {
        "qid": qdir.name,
        "run_status": "succeeded" if run_succeeded else ("failed" if run_succeeded is False else "unknown"),
        "opening_model_calls": opening_model_calls,
        "opening_repair_calls": opening_repair_calls,
        "had_opening_repair": opening_repair_calls > 0,
        "sql_repair_chains": sql_repair_chains,
        "sql_repair_attempts": sql_repair_attempts,
        "sql_repair_success": sql_repair_success,
        "agent_turns": len(turn_tokens),
        "model_input_tokens": turn_tokens,
        "model_input_chars": turn_chars,
        "turns_over_budget": over_budget,
        "context_compactions": compactions,
        "context_retries": context_retries,
        "working_set_compacted_turns": ws_compacted,
        "final_answer_seen": final_answer_seen,
    }


def stats(values: list[int]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "median": int(statistics.median(values)),
        "max": max(values),
    }


def summarize_round(round_dir: Path) -> dict:
    qdirs = question_dirs(round_dir)
    per_q = [s for s in (summarize_question(d) for d in qdirs) if s is not None]
    all_tokens = [t for s in per_q for t in s["model_input_tokens"]]
    all_chars = [c for s in per_q for c in s["model_input_chars"]]
    return {
        "round": str(round_dir),
        "questions_with_events": len(per_q),
        "run_succeeded": sum(1 for s in per_q if s["run_status"] == "succeeded"),
        "opening_repairs": {
            "total_repair_calls": sum(s["opening_repair_calls"] for s in per_q),
            "runs_with_repair": sum(1 for s in per_q if s["had_opening_repair"]),
            "total_opening_calls": sum(s["opening_model_calls"] for s in per_q),
        },
        "sql_repairs": {
            "chains": sum(s["sql_repair_chains"] for s in per_q),
            "extra_attempts": sum(s["sql_repair_attempts"] for s in per_q),
            "chains_final_success": sum(s["sql_repair_success"] for s in per_q),
        },
        "context": {
            "agent_turns": sum(s["agent_turns"] for s in per_q),
            "token_dist": stats(all_tokens),
            "char_dist": stats(all_chars),
            "turns_over_budget": sum(s["turns_over_budget"] for s in per_q),
            "compactions": sum(s["context_compactions"] for s in per_q),
            "context_retries": sum(s["context_retries"] for s in per_q),
            "working_set_compacted_turns": sum(s["working_set_compacted_turns"] for s in per_q),
        },
        "per_question": per_q,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="离线汇总评测轮次：修复率 + 上下文尺寸")
    parser.add_argument("rounds", nargs="*", help="轮次目录名（--bench-dir 下）或目录路径")
    parser.add_argument("--bench-dir", type=Path, default=DEFAULT_BENCH_DIR)
    parser.add_argument("--all", action="store_true", help="汇总 bench-dir 下所有含 qXX/events.jsonl 的轮次")
    parser.add_argument("--output", type=Path, help="把完整结果写入 JSON 文件")
    args = parser.parse_args()

    if args.all:
        round_dirs = sorted(p for p in args.bench_dir.iterdir() if p.is_dir() and question_dirs(p))
    else:
        round_dirs = []
        for name in args.rounds:
            p = Path(name)
            round_dirs.append(p if p.is_absolute() and p.exists() else args.bench_dir / name)

    if not round_dirs:
        print("没有找到可汇总的轮次目录", file=sys.stderr)
        sys.exit(1)

    results = [summarize_round(d) for d in round_dirs]

    for r in results:
        name = Path(r["round"]).name
        c = r["context"]
        print(f"== {name} ==")
        print(
            f"  题数 {r['questions_with_events']} | run 成功 {r['run_succeeded']}"
            f" | Opening 修复 {r['opening_repairs']['total_repair_calls']} 次(涉及 {r['opening_repairs']['runs_with_repair']} 题)"
            f" | SQL 修复链 {r['sql_repairs']['chains']} 条(最终成功 {r['sql_repairs']['chains_final_success']})"
        )
        print(
            f"  回合 {c['agent_turns']} | token min/中位/max = {c['token_dist'].get('min')}/{c['token_dist'].get('median')}/{c['token_dist'].get('max')}"
            f" | 超预算回合 {c['turns_over_budget']} | 压缩 {c['compactions']} 次 | context_retry {c['context_retries']} 次"
        )
    print(f"\n共 {len(results)} 轮")

    if args.output:
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整结果已写入 {args.output}")


if __name__ == "__main__":
    main()
