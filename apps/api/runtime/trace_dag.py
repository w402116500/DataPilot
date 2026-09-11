"""从当前 Run 的持久化事实构造只读 Trace DAG。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from contracts.run_events import RunEventType
from contracts.trace_dag import (
    TraceDagActionKind,
    TraceDagActionRecord,
    TraceDagEdge,
    TraceDagEdgeKind,
    TraceDagNode,
    TraceDagNodeKind,
    TraceDagRead,
    TraceDagRelationshipStatus,
    TraceDagScalar,
    TraceDagSection,
)
from metadata.models import (
    ArtifactModel,
    RunEventModel,
    RunModel,
    SqlAuditLogModel,
    ToolCallModel,
)

_TERMINAL_TYPES = {
    RunEventType.RUN_SUCCEEDED.value,
    RunEventType.RUN_FAILED.value,
    RunEventType.RUN_CANCELED.value,
}
_FINAL_ANSWER_TYPES = {
    RunEventType.FINAL_ANSWER_REQUEST_STARTED.value,
    RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED.value,
    RunEventType.FINAL_ANSWER_VALIDATION_FAILED.value,
    RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT.value,
    RunEventType.ANSWER_READY.value,
}
_EXECUTABLE_KINDS = {
    TraceDagNodeKind.PREPARATION,
    TraceDagNodeKind.AGENT_TURN,
    TraceDagNodeKind.TOOL,
    TraceDagNodeKind.FINAL_ANSWER,
    TraceDagNodeKind.RUN_TERMINAL,
}


class TraceDagBuilder:
    """Builder 只做确定性内存投影，不读取数据库或调用运行依赖。"""

    def build(
        self,
        *,
        run: RunModel,
        events: Iterable[RunEventModel],
        tool_calls: Iterable[ToolCallModel],
        audits: Iterable[SqlAuditLogModel],
        artifacts: Iterable[ArtifactModel],
    ) -> TraceDagRead:
        all_events = list(events)
        all_tools = list(tool_calls)
        all_audits = list(audits)
        all_artifacts = list(artifacts)
        warnings: set[str] = set()
        ordered_events = sorted(
            [item for item in all_events if getattr(item, "run_id", run.id) == run.id],
            key=lambda item: item.seq,
        )
        scoped_tools = [item for item in all_tools if item.run_id == run.id]
        scoped_audits = [item for item in all_audits if item.run_id == run.id]
        scoped_artifacts = [item for item in all_artifacts if item.run_id == run.id]
        if any(getattr(item, "run_id", run.id) != run.id for item in all_events):
            warnings.add("TRACE_DAG_EVENT_RUN_MISMATCH")
        if any(item.run_id != run.id for item in all_tools):
            warnings.add("TRACE_DAG_TOOL_RUN_MISMATCH")
        if any(item.run_id != run.id for item in all_audits):
            warnings.add("TRACE_DAG_SQL_AUDIT_RUN_MISMATCH")
        if any(item.run_id != run.id for item in all_artifacts):
            warnings.add("TRACE_DAG_ARTIFACT_RUN_MISMATCH")
        nodes: dict[str, TraceDagNode] = {}

        self._add_run_start(run, ordered_events, nodes)
        self._add_event_nodes(run, ordered_events, nodes, warnings)
        self._merge_tools(run, scoped_tools, scoped_audits, nodes, warnings)
        self._add_artifacts(run, ordered_events, scoped_artifacts, nodes)
        self._add_terminal_from_run(run, nodes)

        edges = self._build_edges(run.id, nodes, warnings)
        return TraceDagRead(
            run_id=run.id,
            nodes=sorted(nodes.values(), key=_node_sort_key),
            edges=sorted(
                edges.values(), key=lambda item: (item.source, item.target, item.kind.value)
            ),
            sections=self._build_sections(run.id, nodes),
            warnings=sorted(warnings),
        )

    @staticmethod
    def _add_run_start(
        run: RunModel, events: list[RunEventModel], nodes: dict[str, TraceDagNode]
    ) -> None:
        lifecycle = [
            item
            for item in events
            if item.event_type in {RunEventType.RUN_QUEUED.value, RunEventType.RUN_STARTED.value}
        ]
        records = [
            _action(
                run.id,
                item,
                TraceDagActionKind.RUN_STARTED,
                "Run queued" if item.event_type == RunEventType.RUN_QUEUED.value else "Run started",
                status="queued" if item.event_type == RunEventType.RUN_QUEUED.value else "running",
            )
            for item in lifecycle
        ]
        nodes[_start_id(run.id)] = TraceDagNode(
            id=_start_id(run.id),
            kind=TraceDagNodeKind.RUN_START,
            run_id=run.id,
            label="Run start",
            start_seq=lifecycle[0].seq if lifecycle else None,
            end_seq=lifecycle[-1].seq if lifecycle else None,
            status="running" if run.status in {"queued", "running"} else run.status,
            summary="Current Run execution started",
            detail_event_seq=lifecycle[-1].seq if lifecycle else None,
            action_records=records,
            detail={"run_status": run.status},
        )

    def _add_event_nodes(
        self,
        run: RunModel,
        events: list[RunEventModel],
        nodes: dict[str, TraceDagNode],
        warnings: set[str],
    ) -> None:
        for event in events:
            payload = event.payload_json or {}
            if event.event_type.startswith("run.preparation."):
                phase = _safe_id(payload.get("phase"))
                if phase is None:
                    warnings.add("TRACE_DAG_PREPARATION_ID_MISSING")
                    continue
                self._merge_preparation(run.id, event, phase, nodes)
            elif event.event_type == RunEventType.RUN_PROTOCOL_SELECTED.value:
                self._merge_protocol_selection(run.id, event, nodes)
            elif event.event_type.startswith("agent.turn."):
                turn_no = _positive_int(payload.get("turn_no"))
                if turn_no is None:
                    warnings.add("TRACE_DAG_TURN_NO_INVALID")
                    continue
                self._merge_turn(run.id, event, turn_no, nodes)
            elif event.event_type in {
                RunEventType.TOOL_CALLED.value,
                RunEventType.TOOL_SUCCEEDED.value,
                RunEventType.TOOL_FAILED.value,
                RunEventType.ANALYSIS_DISCOVERY_OBSERVED.value,
            }:
                tool_call_id = _safe_id(payload.get("tool_call_id"))
                if tool_call_id is None:
                    warnings.add("TRACE_DAG_TOOL_CALL_ID_MISSING")
                    continue
                self._merge_tool_event(run.id, event, tool_call_id, nodes, warnings)
            elif event.event_type in _FINAL_ANSWER_TYPES:
                self._merge_final_answer(run.id, event, nodes)
            elif event.event_type in _TERMINAL_TYPES:
                self._merge_terminal(run.id, event, nodes)

    @staticmethod
    def _merge_preparation(
        run_id: str,
        event: RunEventModel,
        phase: str,
        nodes: dict[str, TraceDagNode],
    ) -> None:
        node_id = f"run:{run_id}:preparation:{phase}"
        payload = event.payload_json or {}
        completed = event.event_type == RunEventType.RUN_PREPARATION_COMPLETED.value
        status = _text(payload.get("status")) or ("completed" if completed else "running")
        record = _action(
            run_id,
            event,
            TraceDagActionKind.PREPARATION_COMPLETED
            if completed
            else TraceDagActionKind.PREPARATION_STARTED,
            f"Preparation {phase} {'completed' if completed else 'started'}",
            status=status,
            summary=_elapsed_summary(payload),
            reason=_text(payload.get("failure_code")),
        )
        existing = nodes.get(node_id)
        nodes[node_id] = _merged_node(
            existing,
            TraceDagNode(
                id=node_id,
                kind=TraceDagNodeKind.PREPARATION,
                run_id=run_id,
                label=f"Preparation · {phase.replace('_', ' ')}",
                start_seq=event.seq,
                end_seq=event.seq,
                status=status,
                summary=_elapsed_summary(payload),
                detail_event_seq=event.seq,
                action_records=[record],
                detail=_safe_detail(payload, "phase", "elapsed_ms", "failure_code"),
            ),
        )

    @staticmethod
    def _merge_protocol_selection(
        run_id: str,
        event: RunEventModel,
        nodes: dict[str, TraceDagNode],
    ) -> None:
        node_id = f"run:{run_id}:preparation:run_opening"
        payload = event.payload_json or {}
        protocol_id = _text(payload.get("protocol_id"))
        planning_mode = _text(payload.get("planning_mode"))
        summary = f"Protocol selected: {protocol_id or 'unknown'}"
        record = _action(
            run_id,
            event,
            TraceDagActionKind.PROTOCOL_SELECTED,
            "Run protocol selected",
            status="completed",
            summary=summary,
        )
        incoming = TraceDagNode(
            id=node_id,
            kind=TraceDagNodeKind.PREPARATION,
            run_id=run_id,
            label="Preparation · run opening",
            start_seq=event.seq,
            end_seq=event.seq,
            status="completed",
            summary=summary,
            detail_event_seq=event.seq,
            action_records=[record],
            detail={
                "phase": "run_opening",
                "protocol_id": protocol_id,
                "planning_mode": planning_mode,
            },
        )
        nodes[node_id] = _merged_node(nodes.get(node_id), incoming)

    @staticmethod
    def _merge_turn(
        run_id: str,
        event: RunEventModel,
        turn_no: int,
        nodes: dict[str, TraceDagNode],
    ) -> None:
        node_id = _turn_id(run_id, turn_no)
        payload = event.payload_json or {}
        completed = event.event_type == RunEventType.AGENT_TURN_COMPLETED.value
        status = _text(payload.get("status")) or ("completed" if completed else "running")
        tool_names = _text(payload.get("tool_names"))
        action_kind = _text(payload.get("action_kind"))
        summary = action_kind.replace("_", " ") if action_kind else None
        if tool_names:
            summary = f"{summary or 'tool call'}: {tool_names}"
        record = _action(
            run_id,
            event,
            TraceDagActionKind.TURN_COMPLETED if completed else TraceDagActionKind.TURN_STARTED,
            f"Agent turn {turn_no} {'completed' if completed else 'started'}",
            status=status,
            summary=summary or _elapsed_summary(payload),
            reason=_text(payload.get("failure_code")),
        )
        existing = nodes.get(node_id)
        nodes[node_id] = _merged_node(
            existing,
            TraceDagNode(
                id=node_id,
                kind=TraceDagNodeKind.AGENT_TURN,
                run_id=run_id,
                label=f"Agent turn {turn_no}",
                start_seq=event.seq,
                end_seq=event.seq,
                status=status,
                summary=summary,
                turn_no=turn_no,
                detail_event_seq=event.seq,
                action_records=[record],
                detail=_safe_detail(
                    payload,
                    "turn_no",
                    "elapsed_ms",
                    "action_kind",
                    "tool_names",
                    "tool_call_count",
                    "failure_code",
                    "reasoning",
                    "assistant_output",
                ),
            ),
        )

    @staticmethod
    def _merge_tool_event(
        run_id: str,
        event: RunEventModel,
        tool_call_id: str,
        nodes: dict[str, TraceDagNode],
        warnings: set[str],
    ) -> None:
        node_id = _tool_id(run_id, tool_call_id)
        payload = event.payload_json or {}
        turn_no = _positive_int(payload.get("turn_no"))
        if turn_no is None:
            warnings.add("TRACE_DAG_TOOL_TURN_UNRESOLVED")
        called = event.event_type == RunEventType.TOOL_CALLED.value
        failed = event.event_type == RunEventType.TOOL_FAILED.value
        discovery_observed = event.event_type == RunEventType.ANALYSIS_DISCOVERY_OBSERVED.value
        status = "running" if called else ("failed" if failed else "succeeded")
        tool_name = _text(payload.get("tool_name")) or "tool"
        summary = _tool_event_summary(payload)
        record_kind = (
            TraceDagActionKind.TOOL_REQUESTED if called else TraceDagActionKind.TOOL_COMPLETED
        )
        if tool_name == "commit_analysis_claims" and not called:
            record_kind = TraceDagActionKind.CLAIM_COMMITTED
        if discovery_observed:
            record_kind = TraceDagActionKind.DISCOVERY_OBSERVED
        record = _action(
            run_id,
            event,
            record_kind,
            (
                "Discovery observation recorded"
                if discovery_observed
                else f"{tool_name} {'requested' if called else status}"
            ),
            status=status,
            summary=summary,
            tool_call_id=tool_call_id,
            reason=_text(payload.get("reason_code")) or _text(payload.get("error_code")),
        )
        existing = nodes.get(node_id)
        # Tool relationships are only valid when every event for the same
        # tool_call_id carries a valid turn_no.  Once an event is malformed,
        # keep the node unresolved instead of repairing it from an earlier
        # event or allowing a later valid event to restore the relation.
        tool_relationship_resolved = turn_no is not None and (
            existing is None or existing.relationship_status is TraceDagRelationshipStatus.RESOLVED
        )
        effective_turn = turn_no if tool_relationship_resolved else None
        nodes[node_id] = _merged_node(
            existing,
            TraceDagNode(
                id=node_id,
                kind=TraceDagNodeKind.TOOL,
                run_id=run_id,
                label=tool_name.replace("_", " "),
                start_seq=event.seq,
                end_seq=event.seq,
                status=status,
                summary=summary,
                turn_no=effective_turn,
                tool_call_id=tool_call_id,
                detail_event_seq=event.seq,
                relationship_status=(
                    TraceDagRelationshipStatus.RESOLVED
                    if effective_turn is not None
                    else TraceDagRelationshipStatus.UNRESOLVED
                ),
                action_records=[record],
                detail=_safe_detail(
                    payload,
                    "tool_name",
                    "elapsed_ms",
                    "evidence_count",
                    "error_code",
                    "reason_code",
                    "audit_log_id",
                    "artifact_id",
                    "column_count",
                    "row_count",
                    "rows_truncated",
                ),
            ),
        )

    @staticmethod
    def _merge_final_answer(
        run_id: str, event: RunEventModel, nodes: dict[str, TraceDagNode]
    ) -> None:
        node_id = _answer_id(run_id)
        payload = event.payload_json or {}
        ready = event.event_type == RunEventType.ANSWER_READY.value
        failed = event.event_type in {
            RunEventType.FINAL_ANSWER_VALIDATION_FAILED.value,
            RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT.value,
        }
        status = "succeeded" if ready else ("failed" if failed else "running")
        if event.event_type == RunEventType.FINAL_ANSWER_REQUEST_STARTED.value:
            kind = TraceDagActionKind.ANSWER_REQUESTED
            label = "Final answer requested"
        else:
            kind = TraceDagActionKind.ANSWER_VALIDATED
            label = "Final answer ready" if ready else "Final answer checked"
        record = _action(
            run_id,
            event,
            kind,
            label,
            status=status,
            summary=_answer_summary(payload),
            reason=_text(payload.get("failure_code")) or _text(payload.get("incomplete_reason")),
        )
        existing = nodes.get(node_id)
        nodes[node_id] = _merged_node(
            existing,
            TraceDagNode(
                id=node_id,
                kind=TraceDagNodeKind.FINAL_ANSWER,
                run_id=run_id,
                label="Final answer",
                start_seq=event.seq,
                end_seq=event.seq,
                status=status,
                summary=_answer_summary(payload),
                detail_event_seq=event.seq,
                action_records=[record],
                detail=_safe_detail(
                    payload,
                    "attempt",
                    "mode",
                    "elapsed_ms",
                    "failure_code",
                    "validation_stage",
                    "answer_format",
                    "completion_kind",
                    "incomplete_reason",
                    "artifact_count",
                    "evidence_count",
                ),
            ),
        )

    @staticmethod
    def _merge_terminal(run_id: str, event: RunEventModel, nodes: dict[str, TraceDagNode]) -> None:
        node_id = _terminal_id(run_id)
        payload = event.payload_json or {}
        status = event.event_type.removeprefix("run.")
        record = _action(
            run_id,
            event,
            TraceDagActionKind.TERMINAL_RECORDED,
            f"Run {status}",
            status=status,
            reason=_text(payload.get("error_code")),
        )
        nodes[node_id] = _merged_node(
            nodes.get(node_id),
            TraceDagNode(
                id=node_id,
                kind=TraceDagNodeKind.RUN_TERMINAL,
                run_id=run_id,
                label=f"Run {status}",
                start_seq=event.seq,
                end_seq=event.seq,
                status=status,
                summary=_text(payload.get("error_code")),
                detail_event_seq=event.seq,
                action_records=[record],
                detail=_safe_detail(payload, "error_code"),
            ),
        )

    @staticmethod
    def _merge_tools(
        run: RunModel,
        tools: list[ToolCallModel],
        audits: list[SqlAuditLogModel],
        nodes: dict[str, TraceDagNode],
        warnings: set[str],
    ) -> None:
        audit_by_tool: dict[str, SqlAuditLogModel] = {}
        for audit in audits:
            if audit.tool_call_id:
                audit_by_tool[audit.tool_call_id] = audit
        for tool in tools:
            tool_call_id = tool.tool_call_id
            if not tool_call_id:
                warnings.add("TRACE_DAG_TOOL_CALL_ID_MISSING")
                continue
            node_id = _tool_id(run.id, tool_call_id)
            audit = audit_by_tool.get(tool_call_id)
            if audit is not None and audit.run_id != run.id:
                warnings.add("TRACE_DAG_SQL_AUDIT_RUN_MISMATCH")
                audit = None
            detail: dict[str, TraceDagScalar] = {
                "tool_name": tool.tool_name,
                "tool_status": tool.status,
            }
            summary = _summary_text(tool.output_summary_json)
            if audit is not None:
                detail.update(
                    {
                        "audit_id": audit.id,
                        "audit_status": audit.status,
                        "row_count": audit.row_count,
                        "elapsed_ms": audit.elapsed_ms,
                        "error_code": audit.error_code or audit.blocked_reason_code,
                    }
                )
            if node_id not in nodes:
                warnings.add("TRACE_DAG_TOOL_EVENT_MISSING")
                nodes[node_id] = TraceDagNode(
                    id=node_id,
                    kind=TraceDagNodeKind.TOOL,
                    run_id=run.id,
                    label=tool.tool_name.replace("_", " "),
                    status=tool.status,
                    summary=summary,
                    tool_call_id=tool_call_id,
                    relationship_status=TraceDagRelationshipStatus.UNRESOLVED,
                    detail=detail,
                )
            else:
                node = nodes[node_id]
                nodes[node_id] = node.model_copy(
                    update={
                        "status": tool.status,
                        "summary": summary or node.summary,
                        "detail": {**node.detail, **detail},
                    }
                )

    @staticmethod
    def _add_artifacts(
        run: RunModel,
        events: list[RunEventModel],
        artifacts: list[ArtifactModel],
        nodes: dict[str, TraceDagNode],
    ) -> None:
        event_by_artifact = {
            _safe_id(item.payload_json.get("artifact_id")): item
            for item in events
            if item.event_type == RunEventType.ARTIFACT_CREATED.value
            and _safe_id(item.payload_json.get("artifact_id")) is not None
        }
        for artifact in artifacts:
            event = event_by_artifact.get(artifact.id)
            action_records = []
            if event is not None:
                action_records.append(
                    _action(
                        run.id,
                        event,
                        TraceDagActionKind.ARTIFACT_REGISTERED,
                        f"{artifact.type} artifact registered",
                        status="succeeded",
                        summary=artifact.title,
                        tool_call_id=artifact.tool_call_id,
                        artifact_id=artifact.id,
                    )
                )
            nodes[_artifact_id(run.id, artifact.id)] = TraceDagNode(
                id=_artifact_id(run.id, artifact.id),
                kind=TraceDagNodeKind.ARTIFACT,
                run_id=run.id,
                label=artifact.title,
                start_seq=event.seq if event else None,
                end_seq=event.seq if event else None,
                status="succeeded",
                summary=f"{artifact.type} · {artifact.mime_type}",
                tool_call_id=artifact.tool_call_id,
                artifact_id=artifact.id,
                detail_event_seq=event.seq if event else None,
                action_records=action_records,
                detail={
                    "artifact_type": artifact.type,
                    "mime_type": artifact.mime_type,
                    "size_bytes": artifact.size_bytes,
                    "inline_previewable": _inline_previewable(artifact),
                },
            )

    @staticmethod
    def _add_terminal_from_run(run: RunModel, nodes: dict[str, TraceDagNode]) -> None:
        if run.status not in {"succeeded", "failed", "canceled"}:
            return
        node_id = _terminal_id(run.id)
        if node_id in nodes:
            node = nodes[node_id]
            nodes[node_id] = node.model_copy(
                update={
                    "status": run.status,
                    "summary": run.error_code or node.summary,
                    "detail": {**node.detail, "completion_kind": run.completion_kind},
                }
            )
            return
        nodes[node_id] = TraceDagNode(
            id=node_id,
            kind=TraceDagNodeKind.RUN_TERMINAL,
            run_id=run.id,
            label=f"Run {run.status}",
            status=run.status,
            summary=run.error_code,
            detail={"error_code": run.error_code, "completion_kind": run.completion_kind},
        )

    @staticmethod
    def _build_edges(
        run_id: str,
        nodes: dict[str, TraceDagNode],
        warnings: set[str],
    ) -> dict[str, TraceDagEdge]:
        edges: dict[str, TraceDagEdge] = {}

        def add(source: str, target: str, kind: TraceDagEdgeKind) -> None:
            if source not in nodes or target not in nodes or source == target:
                return
            edge_id = f"{source}->{target}:{kind.value}"
            edges[edge_id] = TraceDagEdge(
                id=edge_id,
                source=source,
                target=target,
                kind=kind,
                label=kind.value.replace("_", " "),
            )

        executable = sorted(
            (
                node
                for node in nodes.values()
                if node.kind in _EXECUTABLE_KINDS and node.start_seq is not None
            ),
            key=_node_sort_key,
        )
        if executable:
            add(_start_id(run_id), executable[0].id, TraceDagEdgeKind.STARTS)

        for node in nodes.values():
            if node.kind is TraceDagNodeKind.TOOL:
                if node.turn_no is not None:
                    turn_id = _turn_id(run_id, node.turn_no)
                    if turn_id in nodes:
                        add(turn_id, node.id, TraceDagEdgeKind.INVOKES)
                    else:
                        warnings.add("TRACE_DAG_TOOL_TURN_MISSING")
                else:
                    warnings.add("TRACE_DAG_TOOL_TURN_UNRESOLVED")
            elif node.kind is TraceDagNodeKind.ARTIFACT and node.tool_call_id:
                tool_id = _tool_id(run_id, node.tool_call_id)
                if tool_id in nodes:
                    add(tool_id, node.id, TraceDagEdgeKind.PRODUCES_ARTIFACT)
                else:
                    warnings.add("TRACE_DAG_ARTIFACT_TOOL_MISSING")

        answer = nodes.get(_answer_id(run_id))
        terminal = nodes.get(_terminal_id(run_id))
        answer_ready = bool(
            answer and any(record.label == "Final answer ready" for record in answer.action_records)
        )
        if answer_ready and terminal is not None:
            if (
                answer.end_seq is None
                or terminal.start_seq is None
                or answer.end_seq < terminal.start_seq
            ):
                add(answer.id, terminal.id, TraceDagEdgeKind.COMPLETES)

        semantic_pairs = {(edge.source, edge.target) for edge in edges.values()}
        for source, target in zip(executable, executable[1:], strict=False):
            if (source.id, target.id) not in semantic_pairs:
                add(source.id, target.id, TraceDagEdgeKind.CONTINUES)
        return edges

    @staticmethod
    def _build_sections(run_id: str, nodes: dict[str, TraceDagNode]) -> list[TraceDagSection]:
        grouped: dict[str, list[TraceDagNode]] = {}
        for node in nodes.values():
            if node.start_seq is None or node.kind in {
                TraceDagNodeKind.RUN_START,
                TraceDagNodeKind.ARTIFACT,
            }:
                continue
            if node.kind is TraceDagNodeKind.AGENT_TURN:
                key = f"turn:{node.turn_no}"
            elif node.kind is TraceDagNodeKind.TOOL and node.turn_no is not None:
                key = f"turn:{node.turn_no}"
            else:
                key = node.kind.value
            grouped.setdefault(key, []).append(node)
        sections = []
        for key, section_nodes in grouped.items():
            ordered = sorted(section_nodes, key=_node_sort_key)
            sections.append(
                TraceDagSection(
                    id=f"run:{run_id}:section:{key}",
                    title=(
                        f"Agent turn {ordered[0].turn_no}"
                        if key.startswith("turn:")
                        else ordered[0].label
                    ),
                    status=ordered[-1].status,
                    start_seq=min(node.start_seq or 1 for node in ordered),
                    end_seq=max(node.end_seq or node.start_seq or 1 for node in ordered),
                    node_ids=[node.id for node in ordered],
                )
            )
        return sorted(sections, key=lambda item: (item.start_seq, item.id))


def _merged_node(existing: TraceDagNode | None, incoming: TraceDagNode) -> TraceDagNode:
    if existing is None:
        return incoming
    records = {record.id: record for record in existing.action_records}
    records.update({record.id: record for record in incoming.action_records})
    is_tool_node = existing.kind is TraceDagNodeKind.TOOL or incoming.kind is TraceDagNodeKind.TOOL
    if is_tool_node:
        relationship_status = (
            TraceDagRelationshipStatus.RESOLVED
            if existing.relationship_status is TraceDagRelationshipStatus.RESOLVED
            and incoming.relationship_status is TraceDagRelationshipStatus.RESOLVED
            else TraceDagRelationshipStatus.UNRESOLVED
        )
        merged_turn_no = (
            incoming.turn_no or existing.turn_no
            if relationship_status is TraceDagRelationshipStatus.RESOLVED
            else None
        )
    else:
        relationship_status = (
            TraceDagRelationshipStatus.RESOLVED
            if existing.relationship_status is TraceDagRelationshipStatus.RESOLVED
            or incoming.relationship_status is TraceDagRelationshipStatus.RESOLVED
            else TraceDagRelationshipStatus.UNRESOLVED
        )
        merged_turn_no = incoming.turn_no or existing.turn_no
    return existing.model_copy(
        update={
            "start_seq": min(
                value for value in (existing.start_seq, incoming.start_seq) if value is not None
            ),
            "end_seq": max(
                value for value in (existing.end_seq, incoming.end_seq) if value is not None
            ),
            "status": incoming.status or existing.status,
            "summary": incoming.summary or existing.summary,
            "turn_no": merged_turn_no,
            "detail_event_seq": incoming.detail_event_seq or existing.detail_event_seq,
            "relationship_status": relationship_status,
            "action_records": sorted(records.values(), key=lambda item: (item.event_seq, item.id)),
            "detail": {**existing.detail, **incoming.detail},
        }
    )


def _action(
    run_id: str,
    event: RunEventModel,
    kind: TraceDagActionKind,
    label: str,
    *,
    status: str | None = None,
    summary: str | None = None,
    tool_call_id: str | None = None,
    artifact_id: str | None = None,
    reason: str | None = None,
) -> TraceDagActionRecord:
    return TraceDagActionRecord(
        id=f"run:{run_id}:action:{event.seq}:{event.event_type}",
        kind=kind,
        event_seq=event.seq,
        status=status,
        label=label,
        summary=summary,
        tool_call_id=tool_call_id,
        artifact_id=artifact_id,
        reason=reason,
    )


def _safe_detail(payload: dict[str, Any], *keys: str) -> dict[str, TraceDagScalar]:
    return {
        key: value
        for key in keys
        if (value := payload.get(key)) is None or isinstance(value, str | int | float | bool)
    }


def _tool_event_summary(payload: dict[str, Any]) -> str | None:
    if "row_count" in payload or "column_count" in payload:
        return _summary_text(payload)
    raw = payload.get("output_summary_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return _summary_text(parsed)
    return _text(payload.get("error_code"))


def _summary_text(summary: dict[str, Any] | None) -> str | None:
    if not summary:
        return None
    preferred = (
        "row_count",
        "column_count",
        "output_count",
        "evidence_count",
        "claim_count",
        "fact_count",
    )
    parts = [f"{key.replace('_', ' ')}: {summary[key]}" for key in preferred if key in summary]
    status = summary.get("validation_status") or summary.get("sandbox_status")
    if isinstance(status, str):
        parts.append(status)
    return " · ".join(parts)[:500] or None


def _answer_summary(payload: dict[str, Any]) -> str | None:
    parts = []
    for key in ("mode", "answer_format", "completion_kind", "validation_stage"):
        value = _text(payload.get(key))
        if value:
            parts.append(value.replace("_", " "))
    return " · ".join(parts)[:500] or _elapsed_summary(payload)


def _elapsed_summary(payload: dict[str, Any]) -> str | None:
    elapsed = payload.get("elapsed_ms")
    return f"{elapsed} ms" if isinstance(elapsed, int) and not isinstance(elapsed, bool) else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10_000:
        return value
    return None


def _safe_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 120:
        return None
    return value if all(character.isalnum() or character in "_.:-" for character in value) else None


def _text(value: object) -> str | None:
    return value[:500] if isinstance(value, str) and value else None


def _inline_previewable(artifact: ArtifactModel) -> bool:
    if artifact.type == "table":
        return artifact.storage_ref is not None or artifact.preview_json is not None
    if artifact.type == "chart":
        return artifact.storage_ref is not None and artifact.mime_type in {
            "image/png",
            "image/svg+xml",
        }
    if artifact.type == "markdown":
        return artifact.storage_ref is not None and artifact.mime_type == "text/markdown"
    return artifact.storage_ref is not None and artifact.mime_type in {
        "application/json",
        "text/csv",
        "text/plain",
        "text/tab-separated-values",
    }


def _node_sort_key(node: TraceDagNode) -> tuple[int, int, str]:
    kind_order = {
        TraceDagNodeKind.RUN_START: 0,
        TraceDagNodeKind.PREPARATION: 1,
        TraceDagNodeKind.AGENT_TURN: 2,
        TraceDagNodeKind.TOOL: 3,
        TraceDagNodeKind.ARTIFACT: 4,
        TraceDagNodeKind.FINAL_ANSWER: 5,
        TraceDagNodeKind.RUN_TERMINAL: 6,
    }
    return (node.start_seq or 2**31, kind_order[node.kind], node.id)


def _start_id(run_id: str) -> str:
    return f"run:{run_id}:start"


def _turn_id(run_id: str, turn_no: int) -> str:
    return f"run:{run_id}:turn:{turn_no}"


def _tool_id(run_id: str, tool_call_id: str) -> str:
    return f"run:{run_id}:tool:{tool_call_id}"


def _artifact_id(run_id: str, artifact_id: str) -> str:
    return f"run:{run_id}:artifact:{artifact_id}"


def _answer_id(run_id: str) -> str:
    return f"run:{run_id}:final-answer"


def _terminal_id(run_id: str) -> str:
    return f"run:{run_id}:terminal"
