import { describe, expect, it } from "vitest";

import type { RunArtifact, SqlAudit, ToolCall } from "@/api/types";
import { buildRunInspectionGroups, inspectionGroupForRef, inspectionGroupSummary } from "./runInspection";

function tool(id = "tool_1"): ToolCall {
  return {
    id, run_id: "run_1", tool_name: "run_sql_readonly", status: "succeeded",
    input_params: { sql: "SELECT * FROM orders" }, output_summary: null,
    error_code: null, error_message: null,
    started_at: "2026-09-05T00:00:00Z", finished_at: "2026-09-05T00:00:01Z",
  };
}

function audit(id = "audit_1", toolId: string | null = "tool_1", artifactId: string | null = null): SqlAudit {
  return {
    id, run_id: "run_1", tool_call_id: toolId, datasource_id: "datasource_1",
    datasource_deleted: false, schema_revision: 1, attempt_no: 0, repaired_from_id: null,
    original_sql: "SELECT * FROM orders", normalized_sql: "SELECT * FROM orders LIMIT 200",
    status: "succeeded", statement_type: "select", referenced_tables: ["orders"],
    blocked_reason_code: null, blocked_reason: null, artifact_id: artifactId,
    row_count: 3, elapsed_ms: 36, error_code: null, error_message: null,
    created_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:00:01Z",
  };
}

function artifact(id = "artifact_1", toolId: string | null = "tool_1"): RunArtifact {
  return {
    id, run_id: "run_1", session_id: "session_1", tool_call_id: toolId,
    datasource_deleted: false, type: "table", title: "查询结果", mime_type: "application/json",
    size_bytes: 42, inline_previewable: false, preview: null, metadata: null,
    content_hash: "test_hash", created_at: "2026-09-05T00:00:01Z",
  };
}

describe("Run inspection groups", () => {
  it("resolves audit and result references to the same persisted SQL call", () => {
    const tools = [tool()];
    const audits = [audit()];
    const artifacts = [artifact()];
    const groups = buildRunInspectionGroups("run_1", tools, audits, artifacts);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ id: "tool:tool_1", tool: tools[0], audits, artifacts });
    for (const ref of ["tool_1", "tool:tool_1", "audit_1", "audit:audit_1", "artifact_1", "artifact:artifact_1"]) {
      expect(inspectionGroupForRef(groups, ref)).toBe(groups[0]);
    }
    expect(inspectionGroupSummary(groups[0]!)).toBe("orders · 1 个结果");
  });

  it("does not combine identically titled results from different tool calls", () => {
    const groups = buildRunInspectionGroups("run_1", [tool(), tool("tool_2")], [audit(), audit("audit_2", "tool_2")], [artifact(), artifact("artifact_2", "tool_2")]);
    expect(groups).toHaveLength(2);
    expect(groups.map((group) => [group.tool?.id, group.audits.map((item) => item.id), group.artifacts.map((item) => item.id)])).toEqual([
      ["tool_1", ["audit_1"], ["artifact_1"]],
      ["tool_2", ["audit_2"], ["artifact_2"]],
    ]);
    expect(inspectionGroupForRef(groups, "artifact_2")).toBe(groups[1]);
  });

  it("isolates another Run even when its records reuse the same tool identifier", () => {
    const groups = buildRunInspectionGroups("run_1", [tool(), { ...tool(), run_id: "run_other" }],
      [audit(), { ...audit("audit_other"), run_id: "run_other" }],
      [artifact(), { ...artifact("artifact_other"), run_id: "run_other" }]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.audits.map((item) => item.id)).toEqual(["audit_1"]);
    expect(groups[0]!.artifacts.map((item) => item.id)).toEqual(["artifact_1"]);
    expect(inspectionGroupForRef(groups, "audit_other")).toBeUndefined();
    expect(inspectionGroupForRef(groups, "artifact_other")).toBeUndefined();
  });

  it("keeps records with null provenance independent", () => {
    const groups = buildRunInspectionGroups("run_1", [], [audit("audit_1", null), audit("audit_2", null)], [artifact("artifact_1", null), artifact("artifact_2", null)]);
    expect(groups.map((group) => [group.id, group.audits.length, group.artifacts.length])).toEqual([
      ["audit:audit_1", 1, 0], ["audit:audit_2", 1, 0],
      ["artifact:artifact_1", 0, 1], ["artifact:artifact_2", 0, 1],
    ]);
  });

  it("uses a unique audit artifact_id when the ToolCall or artifact source is unavailable", () => {
    const groups = buildRunInspectionGroups("run_1", [], [audit("audit_1", "missing_tool", "artifact_1")], [artifact("artifact_1", null), artifact("artifact_2", null)]);
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ id: "audit:audit_1", tool: null });
    expect(groups[0]!.artifacts.map((item) => item.id)).toEqual(["artifact_1"]);
    expect(inspectionGroupForRef(groups, "artifact:artifact_1")).toBe(inspectionGroupForRef(groups, "audit:audit_1"));
    expect(groups[1]!.id).toBe("artifact:artifact_2");
  });

  it("does not guess which audit owns an artifact when multiple groups reference it", () => {
    const groups = buildRunInspectionGroups("run_1", [], [audit("audit_1", null, "artifact_1"), audit("audit_2", null, "artifact_1")], [artifact("artifact_1", null)]);
    expect(groups).toHaveLength(3);
    expect(inspectionGroupForRef(groups, "artifact_1")).toMatchObject({ id: "artifact:artifact_1", tool: null, audits: [] });
  });
});
