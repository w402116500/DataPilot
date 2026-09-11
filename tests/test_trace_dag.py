from __future__ import annotations

from types import SimpleNamespace

from runtime.trace_dag import TraceDagBuilder


def _run(run_id: str = "run_trace", status: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(id=run_id, status=status, completion_kind="completed", error_code=None)


def _event(seq: int, event_type: str, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(seq=seq, event_type=event_type, payload_json=payload)


def _scoped_event(seq: int, event_type: str, *, run_id: str, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq,
        run_id=run_id,
        event_type=event_type,
        payload_json=payload,
    )


def _tool(
    tool_call_id: str,
    *,
    run_id: str = "run_trace",
    tool_name: str = "run_sql_readonly",
    status: str = "succeeded",
    summary: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"db_{tool_call_id}",
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        status=status,
        output_summary_json=summary,
    )


def _artifact(
    artifact_id: str,
    *,
    run_id: str = "run_trace",
    tool_call_id: str | None = "call_sql",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=artifact_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        type="table",
        title="Orders result",
        mime_type="application/json",
        storage_ref=None,
        size_bytes=20,
        preview_json={"columns": ["id"], "rows": [[1]]},
    )


def test_builder_merges_events_and_links_stable_facts() -> None:
    run = _run()
    events = [
        _event(9, "run.succeeded"),
        _event(8, "answer.ready", completion_kind="completed", evidence_count=1),
        _event(7, "final_answer.request.started", attempt=1, mode="json_schema"),
        _event(6, "artifact.created", artifact_id="artifact_1", artifact_type="table"),
        _event(
            5,
            "tool.succeeded",
            tool_call_id="call_sql",
            tool_name="run_sql_readonly",
            turn_no=2,
            evidence_count=1,
            output_summary_json='{"row_count":1}',
        ),
        _event(4, "tool.called", tool_call_id="call_sql", tool_name="run_sql_readonly", turn_no=2),
        _event(
            3,
            "agent.turn.completed",
            turn_no=2,
            action_kind="tool_call",
            reasoning="先读取受控数据摘要，再提交证据。",
            assistant_output="我将调用 SQL 工具核对这个指标。",
        ),
        _event(2, "agent.turn.started", turn_no=2),
        _event(1, "run.started"),
    ]
    result = TraceDagBuilder().build(
        run=run,
        events=events,
        tool_calls=[_tool("call_sql", summary={"row_count": 1})],
        audits=[],
        artifacts=[_artifact("artifact_1")],
    )

    assert [node.kind.value for node in result.nodes] == [
        "run-start",
        "agent-turn",
        "tool",
        "artifact",
        "final-answer",
        "run-terminal",
    ]
    assert result.nodes[1].start_seq == 2
    assert result.nodes[1].end_seq == 3
    assert [record.event_seq for record in result.nodes[2].action_records] == [4, 5]
    assert any(edge.kind.value == "invokes" for edge in result.edges)
    assert any(edge.kind.value == "produces_artifact" for edge in result.edges)
    assert any(edge.kind.value == "completes" for edge in result.edges)
    assert result.nodes[2].id == "run:run_trace:tool:call_sql"
    assert result.nodes[3].id == "run:run_trace:artifact:artifact_1"
    turn_detail = result.nodes[1].detail
    assert turn_detail["reasoning"] == "先读取受控数据摘要，再提交证据。"
    assert turn_detail["assistant_output"] == "我将调用 SQL 工具核对这个指标。"


def test_builder_marks_discovery_observation_as_non_evidence_tool_action() -> None:
    result = TraceDagBuilder().build(
        run=_run(status="running"),
        events=[
            _event(1, "run.started"),
            _event(2, "agent.turn.started", turn_no=1),
            _event(
                3,
                "tool.called",
                tool_call_id="discovery_call",
                tool_name="run_sql_readonly",
                turn_no=1,
            ),
            _event(
                4,
                "tool.succeeded",
                tool_call_id="discovery_call",
                tool_name="run_sql_readonly",
                turn_no=1,
                evidence_count=0,
            ),
            _event(
                5,
                "analysis.discovery.observed",
                tool_call_id="discovery_call",
                tool_name="run_sql_readonly",
                turn_no=1,
                audit_log_id="audit_discovery",
                artifact_id="artifact_discovery",
                column_count=3,
                row_count=20,
                rows_truncated=True,
            ),
        ],
        tool_calls=[_tool("discovery_call")],
        audits=[],
        artifacts=[],
    )

    tool = next(node for node in result.nodes if node.kind.value == "tool")
    assert [record.kind.value for record in tool.action_records] == [
        "tool_requested",
        "tool_completed",
        "discovery_observed",
    ]
    assert tool.action_records[-1].label == "Discovery observation recorded"
    assert tool.detail["row_count"] == 20
    assert tool.detail["rows_truncated"] is True


def test_builder_does_not_guess_missing_turn_relation() -> None:
    result = TraceDagBuilder().build(
        run=_run(),
        events=[
            _event(1, "run.started"),
            _event(2, "agent.turn.started", turn_no=1),
            _event(3, "tool.called", tool_call_id="legacy_call", tool_name="run_python"),
            _event(4, "tool.succeeded", tool_call_id="legacy_call", tool_name="run_python"),
        ],
        tool_calls=[_tool("legacy_call", tool_name="run_python")],
        audits=[],
        artifacts=[],
    )

    tool = next(node for node in result.nodes if node.kind.value == "tool")
    assert tool.relationship_status.value == "unresolved"
    assert not any(edge.kind.value == "invokes" for edge in result.edges)
    assert "TRACE_DAG_TOOL_TURN_UNRESOLVED" in result.warnings


def test_builder_merges_protocol_selection_into_run_opening_node() -> None:
    result = TraceDagBuilder().build(
        run=_run(status="running"),
        events=[
            _event(1, "run.started"),
            _event(2, "run.preparation.started", phase="run_opening"),
            _event(
                3,
                "run.preparation.completed",
                phase="run_opening",
                status="completed",
                elapsed_ms=10,
            ),
            _event(
                4,
                "run.protocol.selected",
                protocol_id="general-task",
                selection_mode="opening",
            ),
        ],
        tool_calls=[],
        audits=[],
        artifacts=[],
    )

    opening = next(node for node in result.nodes if node.id.endswith(":preparation:run_opening"))
    assert opening.detail["protocol_id"] == "general-task"
    assert [record.kind.value for record in opening.action_records] == [
        "preparation_started",
        "preparation_completed",
        "protocol_selected",
    ]


def test_builder_does_not_restore_a_tool_relation_after_a_missing_turn_no() -> None:
    result = TraceDagBuilder().build(
        run=_run(),
        events=[
            _event(1, "run.started"),
            _event(2, "agent.turn.started", turn_no=1),
            _event(
                3,
                "tool.called",
                tool_call_id="mixed_call",
                tool_name="run_sql_readonly",
                turn_no=1,
            ),
            _event(4, "tool.succeeded", tool_call_id="mixed_call", tool_name="run_sql_readonly"),
        ],
        tool_calls=[_tool("mixed_call")],
        audits=[],
        artifacts=[],
    )

    tool = next(node for node in result.nodes if node.kind.value == "tool")
    assert tool.relationship_status.value == "unresolved"
    assert tool.turn_no is None
    assert not any(edge.kind.value == "invokes" for edge in result.edges)
    assert "TRACE_DAG_TOOL_TURN_UNRESOLVED" in result.warnings


def test_builder_warns_and_filters_foreign_records() -> None:
    result = TraceDagBuilder().build(
        run=_run(),
        events=[
            _scoped_event(1, "run.started", run_id="other_run"),
            _event(2, "run.started"),
        ],
        tool_calls=[_tool("foreign_tool", run_id="other_run")],
        audits=[SimpleNamespace(run_id="other_run", tool_call_id="foreign_tool")],
        artifacts=[_artifact("foreign_artifact", run_id="other_run")],
    )

    assert [node.start_seq for node in result.nodes if node.kind.value == "run-start"] == [2]
    assert "TRACE_DAG_EVENT_RUN_MISMATCH" in result.warnings
    assert "TRACE_DAG_TOOL_RUN_MISMATCH" in result.warnings
    assert "TRACE_DAG_SQL_AUDIT_RUN_MISMATCH" in result.warnings
    assert "TRACE_DAG_ARTIFACT_RUN_MISMATCH" in result.warnings
    assert not any("foreign_" in node.id for node in result.nodes)


def test_builder_isolates_foreign_rows_and_filters_unsafe_payloads() -> None:
    result = TraceDagBuilder().build(
        run=_run(),
        events=[
            _event(1, "run.started"),
            _event(
                2,
                "tool.succeeded",
                tool_call_id="call_sql",
                tool_name="run_sql_readonly",
                turn_no=1,
                output_summary_json='{"row_count": 3, "prompt": "secret"}',
            ),
        ],
        tool_calls=[
            _tool("call_sql", summary={"row_count": 3}),
            _tool("call_sql", run_id="other_run"),
        ],
        audits=[],
        artifacts=[_artifact("foreign_artifact", run_id="other_run")],
    )

    assert all(node.run_id == "run_trace" for node in result.nodes)
    assert all("prompt" not in node.detail for node in result.nodes)
    assert all("storage_ref" not in node.detail for node in result.nodes)
    assert not any("foreign_artifact" in node.id for node in result.nodes)
