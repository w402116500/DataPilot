import { describe, expect, it } from "vitest";

import type { Run, RunArtifact, SqlAudit, ToolCall } from "@/api/types";

import { createEvidenceCatalog, isEvidenceActionable, resolveAnswerEvidence } from "./evidenceResolver";

const run: Run = {
  id: "run_1",
  session_id: "session_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    user_message_id: null,
    question: "统计销售额",
  status: "succeeded",
  protocol_id: "data-analysis",
  model_profile_id: "model_1",
  model_provider: "openai-compatible",
  model_name: "demo",
  schema_revision: 3,
  datalink_graph_version: null,
  run_timeout_seconds: 60,
  completion_kind: "completed",
  incomplete_reason: null,
  error_code: null,
  error_message: null,
  cancel_requested_at: null,
  cancel_reason: null,
  started_at: "2026-08-16T00:00:00Z",
  finished_at: "2026-08-16T00:00:01Z",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:01Z",
};

function tool(overrides: Partial<ToolCall> = {}): ToolCall {
  return {
    id: "tool_1",
    run_id: "run_1",
    tool_name: "run_sql_readonly",
    status: "succeeded",
    input_params: { sql: "SELECT 1" },
    output_summary: null,
    error_code: null,
    error_message: null,
    started_at: "2026-08-16T00:00:00Z",
    finished_at: "2026-08-16T00:00:01Z",
    ...overrides,
  };
}

function audit(overrides: Partial<SqlAudit> = {}): SqlAudit {
  return {
    id: "audit_1",
    run_id: "run_1",
    tool_call_id: "tool_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    schema_revision: 3,
    attempt_no: 0,
    repaired_from_id: null,
    original_sql: "SELECT 1",
    normalized_sql: "SELECT 1 LIMIT 200",
    status: "succeeded",
    statement_type: "SELECT",
    referenced_tables: [],
    blocked_reason_code: null,
    blocked_reason: null,
    artifact_id: null,
    row_count: 1,
    elapsed_ms: 8,
    error_code: null,
    error_message: null,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:01Z",
    ...overrides,
  };
}

function artifact(overrides: Partial<RunArtifact> = {}): RunArtifact {
  return {
    id: "artifact_1",
    run_id: "run_1",
    session_id: "session_1",
    tool_call_id: "tool_1",
    datasource_deleted: false,
    type: "table",
    title: "销售明细",
    mime_type: "application/json",
    size_bytes: 20,
    inline_previewable: true,
    preview: null,
    metadata: null,
    content_hash: "hash_1",
    created_at: "2026-08-16T00:00:01Z",
    ...overrides,
  };
}

describe("createEvidenceCatalog", () => {
  it("把当前 Run 的 Schema、工具、审计和产物解析为稳定详情目标", () => {
    const catalog = createEvidenceCatalog({
      run,
      toolCalls: [tool()],
      sqlAudits: [audit()],
      artifacts: [artifact()],
    });

    expect(catalog.resolve("schema")).toMatchObject({ kind: "schema", detailId: "schema", label: "数据结构 r3" });
    expect(catalog.resolve("tool_1")).toMatchObject({ kind: "tool", detailId: "tool:tool_1", label: "查询数据" });
    expect(catalog.resolve("audit_1")).toMatchObject({
      kind: "audit",
      detailId: "audit:audit_1",
      label: "第 1 次 SQL 查询",
    });
    expect(catalog.resolve("artifact_1")).toMatchObject({ kind: "artifact", detailId: "artifact:artifact_1" });
  });

  it("拒绝跨 Run 记录，不把旧消息证据接到当前 Run", () => {
    const catalog = createEvidenceCatalog({
      run,
      toolCalls: [tool({ run_id: "run_other" })],
      sqlAudits: [audit({ run_id: "run_other" })],
      artifacts: [artifact({ run_id: "run_other" })],
    });

    expect(catalog.resolve("tool_1")).toMatchObject({ kind: "missing", detailId: null });
    expect(catalog.resolve("audit_1")).toMatchObject({ kind: "missing", detailId: null });
    expect(catalog.resolve("artifact_1")).toMatchObject({ kind: "missing", detailId: null });
  });

  it("同一个 ID 命中多类事实时返回不可操作的冲突项", () => {
    const catalog = createEvidenceCatalog({
      run,
      toolCalls: [tool({ id: "same_id" })],
      sqlAudits: [audit({ id: "same_id" })],
      artifacts: [],
    });

    const result = catalog.resolve("same_id");
    expect(result).toMatchObject({ kind: "ambiguous", detailId: null, label: "证据引用冲突" });
    expect(isEvidenceActionable(result)).toBe(false);
  });

  it("答案级证据至少有一条当前 Run 可用记录时才可用于部分完成答案", () => {
    const catalog = createEvidenceCatalog({
      run,
      toolCalls: [tool()],
      sqlAudits: [],
      artifacts: [],
    });

    const resolved = resolveAnswerEvidence(["tool_1", "missing_ref", "tool_1"], catalog, {
      requireActionableEvidence: true,
    });

    expect(resolved?.refs).toEqual(["tool_1", "missing_ref"]);
    expect(resolved?.evidences.map((evidence) => evidence.kind)).toEqual(["tool", "missing"]);
    expect(resolveAnswerEvidence(["missing_ref"], catalog, { requireActionableEvidence: true })).toBeNull();
  });
});
