"""中文电商基准批量测试驱动：逐题跑真实 Run，落盘完整链路数据供复盘。

用法（在仓库根目录）：
    uv run python scripts/run_benchmark.py --datasource-id <id> --ids q01      # smoke 单题
    uv run python scripts/run_benchmark.py --datasource-id <id>               # 全量 20 题
    uv run python scripts/run_benchmark.py --datasource-id <id> \
        --ids q14,q15 --out-dir .tmp/benchmark-runs/datalink \
        --questions data/benchmark/ecommerce-cn/questions-datalink.jsonl

输出目录（--out-dir，缺省 .tmp/benchmark-runs/<题面文件名>，运行产物不入库）下
每题一个 <qid>/ 子目录：
    run-meta.json      会话/Run/终态/协议/耗时等骨架信息
    events.jsonl       事件流逐条（含 payload，按 seq）
    tool-calls.json    每个工具调用的入参出参
    sql-audits.json    每条 SQL 的审计与结果
    artifacts.json     产物清单（图表/markdown）
    answer.txt         最终答案正文
另写 summary.json 汇总各题骨架信息。

维护约定：题面是唯一真相（见 data/benchmark/ecommerce-cn/NOTES.md）。题面文本
不改；gold 与题面冲突时修 data/benchmark/ecommerce-cn/expected-answers.json。
驱动只读取题面文本与元数据，不参与评分；评分用 scripts/score_benchmark.py。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

DEFAULT_API = "http://127.0.0.1:8011"
DEFAULT_QUESTIONS = Path("data/benchmark/ecommerce-cn/questions.jsonl")
DEFAULT_OUT_ROOT = Path(".tmp/benchmark-runs")
TERMINAL = {"succeeded", "failed", "canceled"}
POLL_SECONDS = 1.5
RUN_TIMEOUT_SECONDS = 420


def load_questions(ids: list[str] | None, questions_path: Path) -> list[dict]:
    questions = []
    for line in questions_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            questions.append(json.loads(line))
    if ids:
        wanted = set(ids)
        found = [q for q in questions if q["id"] in wanted]
        missing = wanted - {q["id"] for q in found}
        if missing:
            raise SystemExit(f"未知题目 id：{sorted(missing)}")
        return found
    return questions


async def request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs):
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


async def run_one(
    client: httpx.AsyncClient,
    question: dict,
    *,
    datasource_id: str,
    out_root: Path,
    session_id: str | None = None,
) -> dict:
    qid = question["id"]
    out_dir = out_root / qid
    out_dir.mkdir(parents=True, exist_ok=True)
    if not session_id:
        session = await request_json(
            client,
            "POST",
            "/sessions",
            json={"title": f"benchmark-{qid}", "selected_datasource_id": datasource_id},
        )
        session_id = session["id"]
    accepted = await request_json(
        client,
        "POST",
        f"/sessions/{session_id}/runs",
        json={
            "question": question["question"],
            "idempotency_key": f"benchmark-{qid}-{int(time.time())}",
        },
    )
    run_id = accepted["run_id"]
    events: list[dict] = []
    after_seq = 0
    started = time.perf_counter()
    while True:
        batch = await request_json(
            client,
            "GET",
            f"/runs/{run_id}/events/history",
            params={"after_seq": after_seq},
        )
        payload = batch if isinstance(batch, list) else batch.get("items", [])
        if isinstance(payload, list) and payload:
            events.extend(payload)
            after_seq = max(int(event["seq"]) for event in payload)
        run = await request_json(client, "GET", f"/runs/{run_id}")
        if isinstance(run, dict) and run.get("status") in TERMINAL:
            break
        if time.perf_counter() - started > RUN_TIMEOUT_SECONDS:
            raise TimeoutError(f"{qid}: run 超过 {RUN_TIMEOUT_SECONDS}s 未到终态")
        await asyncio.sleep(POLL_SECONDS)
    elapsed = round(time.perf_counter() - started, 1)

    tool_calls = await request_json(client, "GET", f"/runs/{run_id}/tool-calls")
    tool_calls = tool_calls if isinstance(tool_calls, list) else tool_calls.get("items", [])
    sql_audits = await request_json(client, "GET", f"/runs/{run_id}/sql-audits")
    sql_audits = sql_audits if isinstance(sql_audits, list) else sql_audits.get("items", [])
    artifacts = await request_json(client, "GET", f"/runs/{run_id}/artifacts")
    artifacts = artifacts if isinstance(artifacts, list) else artifacts.get("items", [])

    with (out_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    (out_dir / "tool-calls.json").write_text(
        json.dumps(tool_calls, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "sql-audits.json").write_text(
        json.dumps(sql_audits, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "artifacts.json").write_text(
        json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    answer_text = "".join(
        event.get("payload", {}).get("delta", "")
        for event in events
        if event.get("type") == "answer.delta"
    ).strip()
    if answer_text:
        (out_dir / "answer.txt").write_text(answer_text, encoding="utf-8")

    def last_of(event_type: str):
        found = [e for e in events if e.get("type") == event_type]
        return found[-1].get("payload", {}) if found else {}

    protocol = last_of("run.protocol.selected")
    meta = {
        "qid": qid,
        "question": question["question"],
        "difficulty": question.get("difficulty"),
        "category": question.get("category"),
        "gold_sql": question.get("gold_sql"),
        "session_id": session_id,
        "run_id": run_id,
        "status": run.get("status"),
        "completion_kind": run.get("completion_kind"),
        "incomplete_reason": run.get("incomplete_reason"),
        "error_code": run.get("error_code"),
        "protocol_id": protocol.get("protocol_id") or run.get("protocol_id"),
        "planning_mode": protocol.get("planning_mode"),
        "elapsed_seconds": elapsed,
        "event_count": len(events),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "sql_audit_count": len(sql_audits) if isinstance(sql_audits, list) else 0,
        "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
        "answer_chars": len(answer_text) if answer_text else 0,
        "trace_dir": str(out_dir).replace("\\", "/"),
    }
    (out_dir / "run-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


async def main() -> None:
    parser = argparse.ArgumentParser(description="中文电商基准批量测试驱动")
    parser.add_argument("--api", default=DEFAULT_API, help="主后端 API 地址")
    parser.add_argument("--datasource-id", required=True, help="被测数据源 ID（环境相关，必填）")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS, help="题目 jsonl 路径")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="输出目录（缺省 .tmp/benchmark-runs/<题面文件名>）",
    )
    parser.add_argument("--ids", default=None, help="逗号分隔的题目 id，缺省全量")
    parser.add_argument("--concurrency", type=int, default=1, help="并行 Run 数，默认串行")
    parser.add_argument(
        "--session-id",
        default=None,
        help="复用既有会话（同 Session 多轮追问验收）；缺省每题新建会话",
    )
    args = parser.parse_args()
    ids = args.ids.split(",") if args.ids else None
    questions = load_questions(ids, args.questions)
    out_root = args.out_dir or (DEFAULT_OUT_ROOT / args.questions.stem)
    out_root.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []
    timeout = httpx.Timeout(30.0, read=60.0)
    # trust_env=False：本驱动只访问本地 API，Windows 系统代理（httpx 经
    # urllib.getproxies 读取注册表）会把 127.0.0.1 请求送进代理导致 502。
    async with httpx.AsyncClient(base_url=args.api, timeout=timeout, trust_env=False) as client:
        if args.concurrency <= 1:
            for question in questions:
                print(f"[{question['id']}] {question['question']}", flush=True)
                try:
                    meta = await run_one(
                        client,
                        question,
                        datasource_id=args.datasource_id,
                        out_root=out_root,
                        session_id=args.session_id,
                    )
                except Exception as exc:  # noqa: BLE001 - 单题失败不终止整批
                    meta = {"qid": question["id"], "driver_error": repr(exc)}
                print(
                    f"    -> {meta.get('status')} / {meta.get('completion_kind')} "
                    f"({meta.get('elapsed_seconds')}s, {meta.get('event_count')} events)",
                    flush=True,
                )
                summary.append(meta)
        else:
            batches = [
                questions[i : i + args.concurrency]
                for i in range(0, len(questions), args.concurrency)
            ]
            for batch in batches:
                results = await asyncio.gather(
                    *(
                        run_one(
                            client,
                            q,
                            datasource_id=args.datasource_id,
                            out_root=out_root,
                            session_id=args.session_id,
                        )
                        for q in batch
                    ),
                    return_exceptions=True,
                )
                for question, result in zip(batch, results, strict=True):
                    if isinstance(result, Exception):
                        summary.append({"qid": question["id"], "driver_error": repr(result)})
                        print(f"    {question['id']} 失败：{result!r}", flush=True)
                    else:
                        summary.append(result)
                        print(
                            f"    {question['id']} -> {result['status']} "
                            f"({result['elapsed_seconds']}s)",
                            flush=True,
                        )
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ok = sum(1 for item in summary if item.get("status") == "succeeded")
    print(f"\n完成：{ok}/{len(summary)} succeeded；明细在 {out_root}/summary.json")


if __name__ == "__main__":
    asyncio.run(main())
