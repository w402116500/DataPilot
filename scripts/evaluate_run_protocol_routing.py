"""Evaluate fixed Run protocol routing predictions, optionally through the real API."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from langsmith import trace

EXPECTED_COUNTS = {
    "general_greeting": 15,
    "general_concept": 20,
    "current_data": 25,
    "schema": 12,
    "discovery": 15,
    "mixed": 15,
    "implicit": 20,
    "data_word_concept": 20,
    "clarification": 12,
    "constraints": 12,
    "adversarial": 12,
}

EXPECTED_MODES = {"general-task", "ready", "discovery", "context_only", "clarification"}
TERMINAL_KINDS = {"completed", "clarification", "partial", "failed", "canceled"}
RESOURCE_KINDS = {"datalink", "sql", "python", "claim", "artifact"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number} must be an object")
        rows.append(row)
    return rows


def validate_corpus(rows: list[dict[str, Any]]) -> None:
    ids = [row.get("sample_id") for row in rows]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("sample_id values must be non-empty and unique")
    counts = Counter(row.get("category") for row in rows)
    if counts != Counter(EXPECTED_COUNTS):
        raise ValueError(f"unexpected category counts: {dict(counts)}")
    for row in rows:
        if row.get("expected_protocol") not in {"general-task", "data-analysis"}:
            raise ValueError(f"invalid expected_protocol for {row['sample_id']}")
        if not isinstance(row.get("requires_current_resources"), bool):
            raise ValueError(f"invalid resource label for {row['sample_id']}")
        if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
            raise ValueError(f"missing rationale for {row['sample_id']}")
        expected_mode = row.get("expected_mode")
        if expected_mode not in EXPECTED_MODES:
            raise ValueError(f"invalid expected_mode for {row['sample_id']}")
        allowed_execution_modes = row.get("allowed_execution_modes", [expected_mode])
        if (
            not isinstance(allowed_execution_modes, list)
            or not allowed_execution_modes
            or any(mode not in EXPECTED_MODES for mode in allowed_execution_modes)
            or len(set(allowed_execution_modes)) != len(allowed_execution_modes)
        ):
            raise ValueError(f"invalid allowed_execution_modes for {row['sample_id']}")
        if not isinstance(row.get("repair_expected"), bool):
            raise ValueError(f"invalid repair_expected for {row['sample_id']}")
        allowed_terminal_kind = row.get("allowed_terminal_kind")
        if (
            not isinstance(allowed_terminal_kind, list)
            or not allowed_terminal_kind
            or any(kind not in TERMINAL_KINDS for kind in allowed_terminal_kind)
        ):
            raise ValueError(f"invalid allowed_terminal_kind for {row['sample_id']}")
        for key in ("max_opening_model_calls", "max_repair_calls"):
            value = row.get(key)
            minimum = 0 if key == "max_repair_calls" else 1
            if (
                not isinstance(value, int)
                or value < minimum
                or (key == "max_repair_calls" and value > 1)
            ):
                raise ValueError(f"invalid {key} for {row['sample_id']}")
        forbidden_resources = row.get("forbidden_resources")
        if (
            not isinstance(forbidden_resources, list)
            or any(resource not in RESOURCE_KINDS for resource in forbidden_resources)
            or len(set(forbidden_resources)) != len(forbidden_resources)
        ):
            raise ValueError(f"invalid forbidden_resources for {row['sample_id']}")


def evaluate(rows: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    corpus_ids = {row["sample_id"] for row in rows}
    by_id = {row["sample_id"]: row for row in predictions}
    missing = [row["sample_id"] for row in rows if row["sample_id"] not in by_id]
    unknown = [row["sample_id"] for row in predictions if row["sample_id"] not in corpus_ids]
    checked = [row for row in rows if row["sample_id"] in by_id]
    correct = sum(
        by_id[row["sample_id"]].get("protocol_id") == row["expected_protocol"] for row in checked
    )
    general_rows = [row for row in checked if row["expected_protocol"] == "general-task"]
    analysis_rows = [row for row in checked if row["expected_protocol"] == "data-analysis"]
    general_misroutes = sum(
        by_id[row["sample_id"]].get("protocol_id") != "general-task" for row in general_rows
    )
    analysis_as_general = sum(
        by_id[row["sample_id"]].get("protocol_id") == "general-task" for row in analysis_rows
    )
    first_opening_legal = sum(
        by_id[row["sample_id"]].get("protocol_id") in {"general-task", "data-analysis"}
        and by_id[row["sample_id"]].get("opening_model_calls") == 1
        and by_id[row["sample_id"]].get("opening_repair_calls") == 0
        for row in checked
    )
    current_data_as_general = sum(
        by_id[row["sample_id"]].get("protocol_id") == "general-task"
        for row in checked
        if row.get("category") == "current_data"
    )
    mixed_as_general = sum(
        by_id[row["sample_id"]].get("protocol_id") == "general-task"
        for row in checked
        if row.get("category") == "mixed"
    )
    unresolved_protocols = sum(
        by_id[row["sample_id"]].get("protocol_id") not in {"general-task", "data-analysis"}
        for row in checked
    )
    mode_mismatches = sum(
        by_id[row["sample_id"]].get("planning_mode") != row["expected_mode"]
        for row in checked
        if by_id[row["sample_id"]].get("protocol_id") in {"data-analysis", "general-task"}
    )
    execution_mode_mismatches = sum(
        by_id[row["sample_id"]].get("planning_mode")
        not in row.get("allowed_execution_modes", [row["expected_mode"]])
        for row in checked
        if by_id[row["sample_id"]].get("planning_mode") is not None
    )
    terminal_kind_mismatches = sum(
        by_id[row["sample_id"]].get("terminal_kind") not in row["allowed_terminal_kind"]
        for row in checked
        if by_id[row["sample_id"]].get("terminal_kind") is not None
    )
    # ``repair_expected`` means that one repair is allowed/anticipated for this
    # sample, not that a repair must happen.  A legal first-round plan must skip
    # repair by contract, so requiring ``opening_repair_calls == 1`` would mark
    # a correct run as a false failure.  The hard upper bound is checked below
    # by ``repair_calls_over_limit`` and each fixture still declares
    # ``max_repair_calls``.
    repair_expectation_mismatches = sum(
        not row["repair_expected"]
        and by_id[row["sample_id"]].get("opening_repair_calls", 0) not in {0, None}
        for row in checked
    )
    forbidden_resource_violations = sum(
        bool(
            set(row.get("forbidden_resources", []))
            & set(by_id[row["sample_id"]].get("resource_kinds", []))
        )
        for row in checked
    )
    return {
        "sample_count": len(rows),
        "predictions": len(predictions),
        "missing_sample_ids": missing,
        "unknown_sample_ids": unknown,
        "protocol_accuracy": correct / len(checked) if checked else 0,
        "first_opening_legal_rate": first_opening_legal / len(checked) if checked else 0,
        "general_misroute_rate": general_misroutes / len(general_rows) if general_rows else 0,
        "analysis_as_general_count": analysis_as_general,
        "analysis_as_general_rate": analysis_as_general / len(analysis_rows)
        if analysis_rows
        else 0,
        "general_as_analysis_count": general_misroutes,
        "current_data_as_general_count": current_data_as_general,
        "mixed_as_general_count": mixed_as_general,
        "unresolved_protocol_count": unresolved_protocols,
        "mode_mismatch_count": mode_mismatches,
        "execution_mode_mismatch_count": execution_mode_mismatches,
        "terminal_kind_mismatch_count": terminal_kind_mismatches,
        "repair_expectation_mismatch_count": repair_expectation_mismatches,
        "forbidden_resource_violation_count": forbidden_resource_violations,
        "opening_calls_over_limit": sum(
            isinstance(by_id[row["sample_id"]].get("opening_model_calls"), int)
            and by_id[row["sample_id"]]["opening_model_calls"] > row["max_opening_model_calls"]
            for row in checked
        ),
        "repair_calls_over_limit": sum(
            isinstance(by_id[row["sample_id"]].get("opening_repair_calls"), int)
            and by_id[row["sample_id"]]["opening_repair_calls"] > row["max_repair_calls"]
            for row in checked
        ),
    }


def _api_data(payload: dict[str, Any]) -> Any:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), (dict, list)):
        raise ValueError("API response does not contain a data envelope")
    return payload["data"]


async def _request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return _api_data(response.json())


def _event_by_type(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == event_type]


def _resource_kinds(events: list[dict[str, Any]], tool_calls: list[dict[str, Any]]) -> list[str]:
    """Project actual resource use without treating tool-call count as a resource type."""

    resources: set[str] = set()
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_name = tool_call.get("tool_name")
        if tool_name == "run_sql_readonly":
            resources.add("sql")
        elif tool_name == "run_python":
            resources.add("python")
        elif tool_name == "explore_datalink":
            resources.add("datalink")
        elif tool_name == "commit_analysis_claims":
            resources.add("claim")
    if _event_by_type(events, "artifact.created"):
        resources.add("artifact")
    return sorted(resources)


def _artifact_types(artifacts: list[dict[str, Any]]) -> list[str]:
    """Project artifact kinds from the public RunArtifactRead contract."""

    return sorted(
        {
            item.get("type")
            for item in artifacts
            if isinstance(item, dict) and isinstance(item.get("type"), str)
        }
    )


async def _evaluate_live_sample(
    client: httpx.AsyncClient,
    *,
    row: dict[str, Any],
    datasource_id: str,
    poll_seconds: float,
    cancel_analysis: bool,
) -> dict[str, Any]:
    sample_id = row["sample_id"]
    session = await _request_json(
        client,
        "POST",
        "/sessions",
        json={"title": f"routing-eval-{sample_id}", "selected_datasource_id": datasource_id},
    )
    session_id = session["id"]
    accepted = await _request_json(
        client,
        "POST",
        f"/sessions/{session_id}/runs",
        json={"question": row["question"], "idempotency_key": f"routing-eval-{sample_id}"},
    )
    run_id = accepted["run_id"]
    events: list[dict[str, Any]] = []
    after_seq = 0
    cancel_sent = False
    terminal = {"succeeded", "failed", "canceled"}
    started_at = time.perf_counter()

    while True:
        new_events = await _request_json(
            client,
            "GET",
            f"/runs/{run_id}/events/history",
            params={"after_seq": after_seq},
        )
        if not isinstance(new_events, list):
            raise ValueError(f"{sample_id}: event history is not a list")
        events.extend(new_events)
        if new_events:
            after_seq = max(int(event["seq"]) for event in new_events)
        run = await _request_json(client, "GET", f"/runs/{run_id}")
        protocol_event = _event_by_type(events, "run.protocol.selected")
        protocol_id = (
            protocol_event[-1].get("payload", {}).get("protocol_id")
            if protocol_event
            else run.get("protocol_id")
        )
        if cancel_analysis and protocol_id == "data-analysis" and not cancel_sent:
            await _request_json(
                client,
                "POST",
                f"/runs/{run_id}/cancel",
                json={"reason": "routing_evaluation"},
            )
            cancel_sent = True
        if run.get("status") in terminal:
            break
        if time.perf_counter() - started_at > 300:
            raise TimeoutError(f"{sample_id}: run did not reach a terminal state")
        await asyncio.sleep(poll_seconds)

    tool_calls = await _request_json(client, "GET", f"/runs/{run_id}/tool-calls")
    sql_audits = await _request_json(client, "GET", f"/runs/{run_id}/sql-audits")
    artifacts = await _request_json(client, "GET", f"/runs/{run_id}/artifacts")
    opening = [
        event
        for event in _event_by_type(events, "run.preparation.completed")
        if event.get("payload", {}).get("phase") == "run_opening"
    ]
    opening_payload = opening[-1].get("payload", {}) if opening else {}
    protocol_events = _event_by_type(events, "run.protocol.selected")
    protocol_payload = protocol_events[-1].get("payload", {}) if protocol_events else {}
    terminal_kind = run.get("completion_kind")
    if terminal_kind is None:
        if run.get("status") == "failed":
            terminal_kind = "failed"
        elif run.get("status") == "canceled":
            terminal_kind = "canceled"
    result = {
        "sample_id": sample_id,
        "run_id": run_id,
        "session_id": session_id,
        "protocol_id": protocol_id,
        "status": run.get("status"),
        "completion_kind": run.get("completion_kind"),
        "terminal_kind": terminal_kind,
        "planning_mode": protocol_payload.get("planning_mode")
        or opening_payload.get("planning_mode"),
        "error_code": run.get("error_code"),
        "opening_model_calls": opening_payload.get("opening_model_calls"),
        "opening_repair_calls": opening_payload.get("opening_repair_calls"),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else None,
        "sql_audit_count": len(sql_audits) if isinstance(sql_audits, list) else None,
        "artifact_count": len(artifacts) if isinstance(artifacts, list) else None,
        "artifact_kinds": _artifact_types(artifacts) if isinstance(artifacts, list) else [],
        "resource_kinds": _resource_kinds(
            events, tool_calls if isinstance(tool_calls, list) else []
        ),
        "event_count": len(events),
        "cancel_sent": cancel_sent,
    }
    try:
        with trace(
            "datapilot.eval.routing",
            run_type="chain",
            inputs={},
            tags=["datapilot", "run-protocol", "evaluation"],
            metadata={
                "sample_id": sample_id,
                "run_id": run_id,
                "protocol_id": protocol_id,
                "opening_model_calls": opening_payload.get("opening_model_calls"),
                "opening_repair_calls": opening_payload.get("opening_repair_calls"),
            },
        ) as evaluation_trace:
            evaluation_trace.end(outputs={"status": run.get("status")})
    except Exception:
        pass
    return result


async def evaluate_live(
    rows: list[dict[str, Any]],
    *,
    api_base_url: str,
    datasource_id: str,
    output: Path,
    limit: int | None,
    categories: set[str],
    sample_ids: set[str],
    poll_seconds: float,
    between_samples_seconds: float,
    max_consecutive_failures: int,
    cancel_analysis: bool,
) -> None:
    selected = [
        row
        for row in rows
        if (not categories or row["category"] in categories)
        and (not sample_ids or row["sample_id"] in sample_ids)
    ]
    if limit is not None:
        selected = selected[:limit]
    output.parent.mkdir(parents=True, exist_ok=True)
    completed_ids = set()
    if output.exists():
        completed_ids = {
            item["sample_id"]
            for item in load_jsonl(output)
            if isinstance(item.get("sample_id"), str)
        }
    timeout = httpx.Timeout(30.0, read=10.0)
    consecutive_failures = 0
    async with httpx.AsyncClient(base_url=api_base_url.rstrip("/"), timeout=timeout) as client:
        with output.open("a", encoding="utf-8") as handle:
            for row in selected:
                if row["sample_id"] in completed_ids:
                    continue
                result = await _evaluate_live_sample(
                    client,
                    row=row,
                    datasource_id=datasource_id,
                    poll_seconds=poll_seconds,
                    cancel_analysis=cancel_analysis,
                )
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                print(json.dumps(result, ensure_ascii=False))
                if result["protocol_id"] not in {"general-task", "data-analysis"}:
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        raise RuntimeError(
                            "stopped after consecutive unresolved protocol results; "
                            "inspect model availability before resuming"
                        )
                else:
                    consecutive_failures = 0
                if between_samples_seconds > 0:
                    await asyncio.sleep(between_samples_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus", type=Path, default=Path("tests/fixtures/run_protocol_routing.jsonl")
    )
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--api-base-url")
    parser.add_argument("--datasource-id")
    parser.add_argument("--output", type=Path, default=Path(".tmp/routing-predictions.jsonl"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--between-samples-seconds", type=float, default=1.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--no-cancel-analysis", action="store_true")
    args = parser.parse_args()
    rows = load_jsonl(args.corpus)
    validate_corpus(rows)
    if args.api_base_url or args.datasource_id:
        if not args.api_base_url or not args.datasource_id:
            parser.error("--api-base-url and --datasource-id must be provided together")
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be positive")
        if args.poll_seconds <= 0:
            parser.error("--poll-seconds must be positive")
        if args.between_samples_seconds < 0:
            parser.error("--between-samples-seconds must not be negative")
        if args.max_consecutive_failures < 1:
            parser.error("--max-consecutive-failures must be positive")
        asyncio.run(
            evaluate_live(
                rows,
                api_base_url=args.api_base_url,
                datasource_id=args.datasource_id,
                output=args.output,
                limit=args.limit,
                categories=set(args.category),
                sample_ids=set(args.sample_id),
                poll_seconds=args.poll_seconds,
                between_samples_seconds=args.between_samples_seconds,
                max_consecutive_failures=args.max_consecutive_failures,
                cancel_analysis=not args.no_cancel_analysis,
            )
        )
        return
    if args.predictions is None:
        print(
            json.dumps(
                {"corpus_valid": True, "sample_count": len(rows), "categories": EXPECTED_COUNTS},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(json.dumps(evaluate(rows, load_jsonl(args.predictions)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
