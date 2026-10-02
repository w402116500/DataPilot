import { describe, expect, it } from "vitest";

import type { Run, RunArtifact, RunEvent, SqlAudit, ToolCall } from "@/api/types";

import { deriveRunTrace } from "./runTrace";

const run: Run = {
  id: "run_1",
  session_id: "session_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    user_message_id: null,
    question: "统计订单",
  status: "succeeded",
  protocol_id: "data-analysis",
  model_profile_id: "model_1",
  model_provider: "openai-compatible",
  model_name: "demo",
  schema_revision: 1,
  datalink_graph_version: null,
  run_timeout_seconds: 60,
  completion_kind: "completed",
  incomplete_reason: null,
  error_code: null,
  error_message: null,
  cancel_requested_at: null,
  cancel_reason: null,
  started_at: "2026-08-16T00:00:00Z",
  finished_at: "2026-08-16T00:00:02Z",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:02Z",
};

function event(
  seq: number,
  type: RunEvent["type"],
  payload: RunEvent["payload"] = {},
  runId = "run_1",
): RunEvent {
  const canonicalPayload = type === "tool.called" || type === "tool.succeeded" || type === "tool.failed"
    ? { turn_no: 1, ...payload }
    : type === "answer.ready"
      ? {
        assistant_message_id: "message_1",
        artifact_count: 0,
        evidence_count: 0,
        answer_format: "markdown",
        completion_kind: "completed",
        claim_audit_summary_json: "[]",
        claim_audit_truncated: false,
        answer_data_freshness: "not_queried",
        historical_context_injected: false,
        historical_summary_count: 0,
        ...payload,
      }
      : payload;
  return { run_id: runId, seq, type, timestamp: `2026-08-16T00:00:0${seq}Z`, payload: canonicalPayload };
}

const tool: ToolCall = {
  id: "tool_1",
  run_id: "run_1",
  tool_name: "run_sql_readonly",
  status: "succeeded",
  input_params: { sql: "SELECT COUNT(*) FROM orders" },
  output_summary: null,
  error_code: null,
  error_message: null,
  started_at: "2026-08-16T00:00:01Z",
  finished_at: "2026-08-16T00:00:02Z",
};

const audit: SqlAudit = {
  id: "audit_1",
  run_id: "run_1",
  tool_call_id: "tool_1",
  datasource_id: "datasource_1",
  datasource_deleted: false,
  schema_revision: 1,
  attempt_no: 0,
  repaired_from_id: null,
  original_sql: "SELECT COUNT(*) FROM orders",
  normalized_sql: "SELECT COUNT(*) FROM orders LIMIT 200",
  status: "succeeded",
  statement_type: "SELECT",
  referenced_tables: ["orders"],
  blocked_reason_code: null,
  blocked_reason: null,
  artifact_id: "artifact_1",
  row_count: 1,
  elapsed_ms: 28,
  error_code: null,
  error_message: null,
  created_at: "2026-08-16T00:00:01Z",
  updated_at: "2026-08-16T00:00:02Z",
};

const artifact: RunArtifact = {
  id: "artifact_1",
  run_id: "run_1",
  session_id: "session_1",
  tool_call_id: "tool_1",
  datasource_deleted: false,
  type: "table",
  title: "订单统计",
  mime_type: "application/json",
  size_bytes: 42,
  inline_previewable: true,
  preview: null,
  metadata: null,
  content_hash: "hash_1",
  created_at: "2026-08-16T00:00:02Z",
};

describe("deriveRunTrace", () => {
  it("labels Discovery observations as planning-only facts", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(1, "analysis.discovery.observed", {
          tool_call_id: "tool_1",
          tool_name: "run_sql_readonly",
          turn_no: 1,
          audit_log_id: "audit_1",
          artifact_id: "artifact_1",
          column_count: 3,
          row_count: 20,
          rows_truncated: true,
        }),
      ],
      toolCalls: [tool],
      sqlAudits: [],
      artifacts: [],
    });

    expect(entries[0]?.title).toBe("已记录受限探索观察");
    expect(entries[0]?.summary).toContain("不是正式证据");
    expect(entries[0]?.statusLabel).toBe("已记录");
  });

  it("只保留当前 Run 事件，并严格按 seq 还原原始账本", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(4, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "run_sql_readonly", elapsed_ms: 28, evidence_count: 2 }),
        event(2, "run.started"),
        event(1, "run.queued"),
        event(3, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
        event(5, "run.succeeded", {}, "run_other"),
      ],
      toolCalls: [tool],
      sqlAudits: [audit],
      artifacts: [artifact],
    });

    expect(entries.map((entry) => entry.seq)).toEqual([1, 2, 3, 4]);
    expect(entries.map((entry) => entry.detailId)).toEqual([
      "event:1",
      "event:2",
      "event:3",
      "event:4",
    ]);
    expect(entries[3]).toMatchObject({
      eventType: "tool.succeeded",
      title: "查询数据执行完成",
      toolCallId: "tool_1",
      statusLabel: "已完成",
    });
    expect(entries[3]?.summary).toContain("关联 1 条 SQL 审计");
    expect(entries[3]?.summary).not.toContain("返回 1 行");
    expect(entries[3]?.resultSummary).toContain("返回 1 行");
  });

  it("只用 artifact_id 打开当前 Run 已登记产物，不按标题或时间猜测", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(1, "artifact.created", { artifact_id: "artifact_1", artifact_type: "table" }),
        event(2, "artifact.created", { artifact_id: "artifact_missing", artifact_type: "file" }),
      ],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [artifact],
    });

    expect(entries[0]).toMatchObject({ artifactId: "artifact_1", title: "生成产物：订单统计" });
    expect(entries[1]).toMatchObject({ artifactId: null, title: "生成产物：file" });
  });

  it("完整保留 Agent 回合、答案和取消终态事件", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(1, "agent.turn.started", { turn_no: 1 }),
        event(2, "agent.turn.completed", { turn_no: 1 }),
        event(3, "answer.delta", { delta: "结论" }),
        event(4, "answer.ready", { artifact_count: 0, completion_kind: "completed" }),
        event(5, "run.cancel_requested", { reason_code: "user_requested" }),
        event(6, "run.canceled", { error_code: "RUN_CANCELED" }),
      ],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
    });

    expect(entries).toHaveLength(6);
    expect(entries.map((entry) => entry.eventType)).toEqual([
      "agent.turn.started",
      "agent.turn.completed",
      "answer.delta",
      "answer.ready",
      "run.cancel_requested",
      "run.canceled",
    ]);
    expect(entries[2]?.summary).toBe("已追加 2 个答案字符。");
    expect(entries[5]).toMatchObject({ status: "canceled", statusLabel: "已取消" });
  });

  it("以安全阶段名和耗时呈现准备事件", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(1, "run.preparation.started", { phase: "semantic_context" }),
        event(2, "run.preparation.completed", { phase: "semantic_context", elapsed_ms: 18 }),
      ],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
    });

    expect(entries[0]).toMatchObject({ title: "开始读取数据地图", statusLabel: "进行中" });
    expect(entries[1]).toMatchObject({ title: "读取数据地图完成", statusLabel: "已完成" });
    expect(entries[1]?.summary).toBe("读取数据地图已完成，用时 18 ms。");
  });

  it("呈现最终答案的受限时序，不展示模型原文", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(1, "final_answer.request.started", { attempt: 1, mode: "json_schema" }),
        event(2, "final_answer.response.received", { attempt: 1, mode: "json_schema", elapsed_ms: 245 }),
        event(3, "final_answer.validation.failed", {
          attempt: 1,
          mode: "json_schema",
          elapsed_ms: 245,
          failure_code: "MODEL_OUTPUT_INVALID",
          validation_stage: "markdown",
        }),
        event(4, "final_answer.request.timed_out", {
          attempt: 2,
          mode: "json_schema",
          elapsed_ms: 45_000,
          failure_code: "FINAL_ANSWER_TIMEOUT",
        }),
      ],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
    });

    expect(entries.map((entry) => entry.title)).toEqual([
      "开始生成最终答案",
      "收到最终答案模型回复",
      "最终答案未通过校验",
      "最终答案生成超时",
    ]);
    expect(entries[1]?.summary).toBe("模型回复已收到，用时 245 ms，正在进行格式和安全校验。");
    expect(entries[2]?.summary).toBe("模型已返回结构化答案，但 Markdown 换行或块结构不合规，原文不会展示。");
    expect(entries[3]?.summary).toBe("本次最终答案请求在 45000 ms 后超过阶段时限，已停止。");
  });

  it("展示脱敏后的 SQL Guard 原因、位置和改写提示", () => {
    const entries = deriveRunTrace({
      run,
      events: [
        event(1, "tool.failed", {
          tool_call_id: "tool_1",
          tool_name: "run_sql_readonly",
          error_code: "DATA_GATEWAY_BLOCKED",
          reason_code: "SQL_PARSE_ERROR",
          error_message: "SQL 无法解析",
          hint: "请将字段逐字包为双引号字段名。",
          retryable: true,
          subject: "AVG(not.fully.paid)",
          line: 1,
          column: 15,
        }),
      ],
      toolCalls: [{ ...tool, status: "failed", error_code: "DATA_GATEWAY_BLOCKED", error_message: "SQL 无法解析" }],
      sqlAudits: [],
      artifacts: [],
    });

    expect(entries[0]?.summary).toBe(
      "SQL 无法解析：AVG(not.fully.paid)（第 1 行，第 15 列）：请将字段逐字包为双引号字段名。系统允许提交一条不同的 SQL 改写。",
    );
    expect(entries[0]?.resultSummary).toBeNull();
  });
});
