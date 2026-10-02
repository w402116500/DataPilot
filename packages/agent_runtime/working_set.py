"""Model-facing history projection; the Run transcript stays unchanged."""

from __future__ import annotations

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


def project_tool_blocks(blocks: Sequence[list[BaseMessage]]) -> list[list[BaseMessage]]:
    """Keep successful facts and only the latest unresolved failure's arguments.

    The caller supplies provider-valid blocks and applies the shared observation
    allowlist and token budget afterwards. No persisted messages are mutated.
    """

    outcomes: dict[str, bool | None] = {}
    names: dict[str, str] = {}
    for block in blocks:
        for message in block:
            if isinstance(message, AIMessage):
                names.update({call["id"]: call["name"] for call in message.tool_calls})
            if isinstance(message, ToolMessage):
                try:
                    payload = json.loads(str(message.content))
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                summary = payload.get("summary", {})
                invalid = isinstance(summary, dict) and (
                    summary.get("validation_status") in {"invalid", "failed"}
                    or summary.get("validation_error_code") is not None
                )
                if payload.get("status") in {"succeeded", "failed", "not_started"}:
                    meaningful = isinstance(summary, dict) and any(
                        key in summary
                        for key in (
                            "verified_values",
                            "row_count",
                            "semantic_context",
                            "output_count",
                            "python_failure",
                            "validation_findings",
                        )
                    )
                    outcomes[message.tool_call_id] = (
                        payload["status"] == "succeeded" and not invalid if meaningful else None
                    )

    latest_by_name: dict[str, str] = {}
    for call_id in outcomes:
        latest_by_name[names.get(call_id, "")] = call_id
    latest_failure = next(
        (
            call_id
            for call_id in reversed(outcomes)
            if outcomes[call_id] is False and latest_by_name.get(names.get(call_id, "")) == call_id
        ),
        None,
    )
    projected: list[list[BaseMessage]] = []
    for block in blocks:
        if not block or not isinstance(block[0], AIMessage) or not block[0].tool_calls:
            projected.append(block)
            continue
        assistant = block[0]
        calls = []
        for call in assistant.tool_calls:
            call_id = call["id"]
            if outcomes.get(call_id) is False and call_id != latest_failure:
                continue
            if outcomes.get(call_id) is True:
                # SQL constraints are in the fixed plan; verified values and
                # binding IDs stay in observations. Executed code is not a fact.
                arguments = {
                    key: value
                    for key, value in call["args"].items()
                    if key in {"requirement_ids", "assertion_ids", "output_paths", "query", "focus"}
                }
                calls.append({**call, "args": arguments})
            elif outcomes.get(call_id) is False:
                calls.append(call)
            else:
                calls.append(call)
        if not calls:
            continue
        kept_ids = {call["id"] for call in calls}
        projected.append(
            [
                assistant.model_copy(
                    update={"content": "", "tool_calls": calls, "additional_kwargs": {}}
                ),
                *[
                    message
                    for message in block[1:]
                    if isinstance(message, ToolMessage) and message.tool_call_id in kept_ids
                ],
            ]
        )
    return projected
