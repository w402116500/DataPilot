import { describe, expect, it } from "vitest";

import { parseRunEvent, parseSseFrame } from "./runEvents";

describe("run event narrowing", () => {
  it("accepts only known event types and scalar payloads", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 2,
      type: "agent.turn.started",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { turn_no: 1 },
    });

    expect(event).toMatchObject({ run_id: "run_1", seq: 2, type: "agent.turn.started" });
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 3,
      type: "answer.delta",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { delta: "候选答案" },
    })).toMatchObject({ run_id: "run_1", seq: 3, type: "answer.delta" });
    const safeEvent = parseRunEvent({
      run_id: "run_1",
      seq: 4,
      type: "tool.succeeded",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { tool_call_id: "tool_1", tool_name: "run_sql_readonly", turn_no: 1, elapsed_ms: 2, evidence_count: 2, output_summary_json: "{}", hidden: { secret: true } },
    });
    expect(safeEvent).toBeNull();
  });

  it("accepts bounded Agent 回合动作摘要并拒绝未注册工具名", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 13,
      type: "agent.turn.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        turn_no: 2,
        elapsed_ms: 1840,
        status: "completed",
        action_kind: "tool_call",
        tool_names: "run_sql_readonly,run_python",
        tool_call_count: 2,
      },
    });
    expect(event?.payload).toMatchObject({
      turn_no: 2,
      elapsed_ms: 1840,
      action_kind: "tool_call",
      tool_names: "run_sql_readonly,run_python",
      tool_call_count: 2,
    });
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 14,
      type: "agent.turn.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { turn_no: 2, status: "completed", action_kind: "tool_call", tool_names: "SELECT *" },
    })).toBeNull();
  });

  it("accepts Working Set and context budget diagnostics emitted by the backend", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 14,
      type: "agent.turn.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        turn_no: 2,
        elapsed_ms: 1840,
        status: "completed",
        action_kind: "respond",
        tool_names: "",
        tool_call_count: 0,
        context_retry_count: 1,
        context_compaction_count: 2,
        working_set_count: 3,
        working_set_compacted: true,
        model_input_chars: 12000,
        model_input_tokens: 3000,
        estimated_total_tokens: 4000,
        context_window_tokens: 8192,
        input_budget_tokens: 7000,
        remaining_tokens: 4192,
        estimate_source: "utf8_estimate",
        working_set_value_count: 4,
      },
    });

    expect(event?.payload).toMatchObject({
      context_retry_count: 1,
      context_compaction_count: 2,
      working_set_count: 3,
      working_set_compacted: true,
      model_input_tokens: 3000,
      estimate_source: "utf8_estimate",
    });
  });

  it("keeps only the safe Discovery observation summary", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 15,
      type: "analysis.discovery.observed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        tool_call_id: "discovery_call",
        tool_name: "run_sql_readonly",
        turn_no: 1,
        audit_log_id: "audit_discovery",
        artifact_id: "artifact_discovery",
        column_count: 3,
        row_count: 20,
        rows_truncated: true,
        sql: "SELECT * FROM source",
        rows: [["must-not-reach-web"]],
      },
    });

    expect(event).toBeNull();
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 16,
      type: "analysis.discovery.observed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        tool_call_id: "discovery_call",
        tool_name: "run_sql_readonly",
        turn_no: 1,
        audit_log_id: "audit_discovery",
        column_count: 3,
        row_count: 20,
        rows_truncated: true,
      },
    })).toBeNull();
  });

  it("parses backend frames and ignores heartbeats or mismatched event names", () => {
    const frame = [
      "id: 4",
      "event: run.succeeded",
      'data: {"run_id":"run_1","seq":4,"type":"run.succeeded","timestamp":"2026-08-14T00:00:00Z","payload":{}}',
      "",
    ].join("\n");
    expect(parseSseFrame(frame)?.type).toBe("run.succeeded");
    expect(parseSseFrame(": heartbeat\n\n")).toBeNull();
    expect(parseSseFrame(frame.replace("event: run.succeeded", "event: run.failed"))).toBeNull();
  });

  it("keeps the partial completion markers from answer.ready", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 5,
      type: "answer.ready",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        assistant_message_id: "message_1",
        artifact_count: 0,
        evidence_count: 0,
        answer_format: "markdown",
        completion_kind: "partial",
        incomplete_reason: "RUN_TIMEOUT",
        claim_audit_summary_json: "[]",
        claim_audit_truncated: false,
        answer_data_freshness: "not_queried",
        historical_context_injected: false,
        historical_summary_count: 0,
      },
    });
    expect(event?.payload.completion_kind).toBe("partial");
    expect(event?.payload.incomplete_reason).toBe("RUN_TIMEOUT");
  });

  it("accepts the bounded claim audit summary and fact mismatch reason", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 12,
      type: "answer.ready",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        assistant_message_id: "message_1",
        artifact_count: 0,
        evidence_count: 1,
        answer_format: "markdown",
        completion_kind: "partial",
        incomplete_reason: "FINAL_ANSWER_FACT_MISMATCH",
        claim_audit_summary_json: "[{\"claim_id\":\"C1\"}]",
        claim_audit_truncated: false,
        answer_data_freshness: "current_run_evidence",
        historical_context_injected: false,
        historical_summary_count: 0,
      },
    });

    expect(event?.payload.incomplete_reason).toBe("FINAL_ANSWER_FACT_MISMATCH");
    expect(event?.payload.claim_audit_summary_json).toBe("[{\"claim_id\":\"C1\"}]");
  });

  it("keeps fixed preparation fields and bounded Opening call counts", () => {
    const completed = parseRunEvent({
      run_id: "run_1",
      seq: 6,
      type: "run.preparation.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { phase: "semantic_context", elapsed_ms: 123, status: "completed", semantic_context: { secret: true } },
    });
    expect(completed).toBeNull();
    const opening = parseRunEvent({
      run_id: "run_1",
      seq: 7,
      type: "run.preparation.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        phase: "run_opening",
        elapsed_ms: 456,
        status: "completed",
        opening_model_calls: 2,
        opening_repair_calls: 1,
      },
    });
    expect(opening?.payload).toMatchObject({ opening_model_calls: 2, opening_repair_calls: 1 });
    const invalidJson = parseRunEvent({
      run_id: "run_1",
      seq: 12,
      type: "run.preparation.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        phase: "analysis_plan",
        elapsed_ms: 6510,
        status: "failed",
        failure_code: "ANALYSIS_PLAN_INVALID",
        validation_issue_count: 1,
        validation_issue_types: "json_invalid",
        validation_issue_reasons: "shape_invalid",
        validation_issue_paths: "response.invalid_tool_calls",
        validation_tool_call_count: 0,
        validation_invalid_tool_call_count: 1,
      },
    });
    expect(invalidJson?.payload).toMatchObject({
      validation_tool_call_count: 0,
      validation_invalid_tool_call_count: 1,
    });
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 8,
      type: "run.preparation.started",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { phase: "prompt-body-must-not-reach-web" },
    })).toBeNull();
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 9,
      type: "run.preparation.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { phase: "run_opening", opening_model_calls: 1, opening_repair_calls: 1 },
    })).toBeNull();
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 10,
      type: "run.preparation.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { phase: "run_opening", opening_model_calls: 3, opening_repair_calls: 1 },
    })).toBeNull();
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 11,
      type: "run.preparation.completed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: { phase: "semantic_context", opening_model_calls: 1, opening_repair_calls: 0 },
    })).toBeNull();
  });

  it("keeps only safe final-answer timing fields", () => {
    const received = parseRunEvent({
      run_id: "run_1",
      seq: 8,
      type: "final_answer.response.received",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        attempt: 1,
        mode: "json_schema",
        elapsed_ms: 245,
        markdown: "must-not-reach-web",
      },
    });
    expect(received).toBeNull();
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 9,
      type: "final_answer.request.timed_out",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        attempt: 2,
        mode: "json_schema",
        elapsed_ms: 45_000,
        failure_code: "MODEL_REQUEST_FAILED",
      },
    })).toBeNull();

    const markdownFailure = parseRunEvent({
      run_id: "run_1",
      seq: 10,
      type: "final_answer.validation.failed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        attempt: 1,
        mode: "json_schema",
        elapsed_ms: 245,
        failure_code: "MODEL_OUTPUT_INVALID",
        validation_stage: "markdown",
        markdown: "must-not-reach-web",
      },
    });
    expect(markdownFailure).toBeNull();
    expect(parseRunEvent({
      run_id: "run_1",
      seq: 11,
      type: "final_answer.validation.failed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        attempt: 1,
        mode: "json_schema",
        elapsed_ms: 245,
        failure_code: "MODEL_OUTPUT_INVALID",
        validation_stage: "raw_model_text",
      },
    })).toBeNull();
    const budgetFailure = parseRunEvent({
      run_id: "run_1",
      seq: 21,
      type: "final_answer.validation.failed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        attempt: 1,
        mode: "markdown",
        elapsed_ms: 12,
        failure_code: "CONTEXT_BUDGET_EXHAUSTED",
        validation_stage: "budget",
      },
    });
    expect(budgetFailure?.payload).toMatchObject({
      failure_code: "CONTEXT_BUDGET_EXHAUSTED",
      validation_stage: "budget",
    });
  });

  it("keeps only the allowlisted SQL Guard failure feedback", () => {
    const event = parseRunEvent({
      run_id: "run_1",
      seq: 6,
      type: "tool.failed",
      timestamp: "2026-08-14T00:00:00Z",
      payload: {
        tool_call_id: "tool_1",
        tool_name: "run_sql_readonly",
        turn_no: 1,
        elapsed_ms: 20,
        output_summary_json: "{}",
        error_code: "DATA_GATEWAY_BLOCKED",
        reason_code: "SQL_PARSE_ERROR",
        error_message: "SQL 无法解析",
        hint: "请将字段逐字包为双引号字段名。",
        retryable: true,
        subject: "AVG(not.fully.paid)",
        line: 1,
        column: 15,
      },
    });

    expect(event?.payload).toEqual({
      tool_call_id: "tool_1",
      tool_name: "run_sql_readonly",
      turn_no: 1,
      elapsed_ms: 20,
      output_summary_json: "{}",
      error_code: "DATA_GATEWAY_BLOCKED",
      reason_code: "SQL_PARSE_ERROR",
      error_message: "SQL 无法解析",
      hint: "请将字段逐字包为双引号字段名。",
      retryable: true,
      subject: "AVG(not.fully.paid)",
      line: 1,
      column: 15,
    });
  });
});
