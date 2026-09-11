"""Build durable DataLink consumption records from the same safe projection Agent sees."""

from __future__ import annotations

import json

from contracts.validation import (
    DATALINK_CONSUMPTION_PAYLOAD_LIMIT_BYTES,
    DATALINK_CONSUMPTION_PAYLOAD_VERSION,
    DataLinkConsumptionCreate,
    DataLinkConsumptionSummary,
)

from agent_runtime.contracts import AgentFailure, DataLinkExploreResponse
from agent_runtime.datalink_semantics import project_datalink_semantic_context


def build_datalink_consumption(
    *,
    run_id: str,
    stage: str,
    query: str,
    focus: str | None,
    max_nodes: int,
    schema_revision: int,
    graph_version: str | None,
    result: DataLinkExploreResponse | AgentFailure | None,
    tool_call_id: str | None = None,
    consume_empty: bool = False,
) -> DataLinkConsumptionCreate:
    """Persist only the projected semantic context; never raw MCP JSON or ToolMessage text."""

    if result is None or isinstance(result, AgentFailure):
        return DataLinkConsumptionCreate(
            run_id=run_id,
            stage=stage,  # type: ignore[arg-type]
            query=query,
            focus=focus,  # type: ignore[arg-type]
            max_nodes=max_nodes,
            schema_revision=schema_revision,
            graph_version=graph_version,
            mode=None,
            payload_status="unavailable",
            returned_status="unavailable",
            consumer_receipt_status="not_received",
            tool_call_id=tool_call_id,
            payload_version=DATALINK_CONSUMPTION_PAYLOAD_VERSION,
            summary=DataLinkConsumptionSummary(),
        )

    context = project_datalink_semantic_context(result)
    payload = context.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    summary = DataLinkConsumptionSummary(
        field_count=len(context.fields),
        relationship_count=len(context.relationships),
        join_path_count=len(context.join_paths),
        warning_count=len(context.warnings),
        payload_bytes=len(encoded),
    )
    truncated = result.result.is_truncated
    if len(encoded) > DATALINK_CONSUMPTION_PAYLOAD_LIMIT_BYTES:
        return DataLinkConsumptionCreate(
            run_id=run_id,
            stage=stage,  # type: ignore[arg-type]
            query=query,
            focus=focus,  # type: ignore[arg-type]
            max_nodes=max_nodes,
            schema_revision=schema_revision,
            graph_version=graph_version or context.graph_version,
            mode=context.mode,
            payload_status="too_large",
            returned_status="too_large",
            consumer_receipt_status="not_received",
            is_truncated=truncated,
            tool_call_id=tool_call_id,
            payload_version=DATALINK_CONSUMPTION_PAYLOAD_VERSION,
            summary=summary,
        )
    empty = not context.fields
    if empty and not consume_empty:
        receipt = "ignored_empty"
        returned = "no_match"
    elif truncated:
        receipt = "received"
        returned = "truncated"
    else:
        receipt = "received"
        returned = "ok" if not empty else "no_match"
    return DataLinkConsumptionCreate(
        run_id=run_id,
        stage=stage,  # type: ignore[arg-type]
        query=query,
        focus=focus,  # type: ignore[arg-type]
        max_nodes=max_nodes,
        schema_revision=schema_revision,
        graph_version=graph_version or context.graph_version,
        mode=context.mode,
        payload_status="complete",
        returned_status=returned,  # type: ignore[arg-type]
        consumer_receipt_status=receipt,  # type: ignore[arg-type]
        is_truncated=truncated,
        tool_call_id=tool_call_id,
        payload_version=DATALINK_CONSUMPTION_PAYLOAD_VERSION,
        semantic_context=context,
        summary=summary,
    )
