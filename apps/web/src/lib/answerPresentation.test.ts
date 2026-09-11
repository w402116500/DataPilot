import { describe, expect, it } from "vitest";

import type { Run, RunArtifact } from "@/api/types";

import { answerProvenanceLabel, chartArtifactsForAnswer, presentAssistantAnswer } from "./answerPresentation";

function run(overrides: Partial<Run>): Run {
  return {
    id: "run_1",
    session_id: "session_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    user_message_id: "message_1",
    question: "问题",
    status: "succeeded",
    protocol_id: "data-analysis",
    model_profile_id: "profile_1",
    model_provider: "openai-compatible",
    model_name: "model",
    schema_revision: 1,
    datalink_graph_version: null,
    run_timeout_seconds: 60,
    completion_kind: "completed",
    incomplete_reason: null,
    error_code: null,
    error_message: null,
    cancel_requested_at: null,
    cancel_reason: null,
    started_at: null,
    finished_at: null,
    created_at: "2026-08-31T00:00:00Z",
    updated_at: "2026-08-31T00:00:00Z",
    ...overrides,
  };
}

function chartArtifact(overrides: Partial<RunArtifact> = {}): RunArtifact {
  return {
    id: "artifact_chart_1",
    run_id: "run_1",
    session_id: "session_1",
    tool_call_id: "tool_1",
    datasource_deleted: false,
    type: "chart",
    title: "支付方式订单数",
    mime_type: "image/png",
    size_bytes: 2048,
    inline_previewable: true,
    preview: null,
    metadata: null,
    content_hash: "hash_chart_1",
    created_at: "2026-09-09T10:59:00Z",
    ...overrides,
  };
}

describe("chartArtifactsForAnswer", () => {
  it("只把同一 Run 可内联预览的图表挂到 Assistant 答案", () => {
    const charts = chartArtifactsForAnswer(
      { role: "assistant", run_id: "run_1" },
      [
        chartArtifact(),
        chartArtifact({
          id: "artifact_table_1",
          type: "table",
          title: "查询结果",
          mime_type: "application/json",
        }),
        chartArtifact({ id: "artifact_other_run", run_id: "run_other" }),
        chartArtifact({
          id: "artifact_download_only",
          inline_previewable: false,
        }),
        chartArtifact({
          id: "artifact_unsafe",
          mime_type: "image/jpeg",
        }),
      ],
    );

    expect(charts).toEqual([expect.objectContaining({ id: "artifact_chart_1", type: "chart" })]);
  });

  it("用户消息、无 run_id 或没有图表时不投影", () => {
    const charts = [chartArtifact()];
    expect(chartArtifactsForAnswer({ role: "user", run_id: "run_1" }, charts)).toEqual([]);
    expect(chartArtifactsForAnswer({ role: "assistant", run_id: null }, charts)).toEqual([]);
    expect(chartArtifactsForAnswer({ role: "assistant", run_id: "run_1" }, [])).toEqual([]);
  });
});

describe("presentAssistantAnswer", () => {
  it("隐藏正式答案中的内部审计和产物编号，同时保留周围事实文字", () => {
    const result = presentAssistantAnswer(
      "查询已审计（audit_[phone]_a82780b2baa1），结果已保存为产物（artifact_a1b2c3d4）。",
    );

    expect(result).toBe("查询已审计（本次审计记录），结果已保存为产物（本次分析产物）。");
    expect(result).not.toMatch(/(?:audit|artifact)_/u);
  });

  it("不改动普通 Markdown 和业务字段名", () => {
    expect(presentAssistantAnswer("**orders** 的 `audit_status` 为 completed")).toBe(
      "**orders** 的 `audit_status` 为 completed",
    );
  });

  it("按服务端 freshness 展示历史来源和本次查询边界", () => {
    expect(
      answerProvenanceLabel(
        run({ answer_data_freshness: "not_queried", historical_context_injected: true }),
      ),
    ).toContain("本次未重新查询");
    expect(
      answerProvenanceLabel(
        run({
          answer_data_freshness: "not_queried",
          historical_context_injected: true,
          historical_summary_count: 0,
        }),
      ),
    ).toContain("历史上下文");
    expect(
      answerProvenanceLabel(
        run({
          answer_data_freshness: "not_queried",
          historical_context_injected: true,
          historical_summary_count: 1,
        }),
      ),
    ).toContain("历史回答摘要");
    expect(answerProvenanceLabel(run({ answer_data_freshness: "current_schema" }))).toContain(
      "Schema",
    );
    expect(
      answerProvenanceLabel(run({ answer_data_freshness: "current_run_observation_only" })),
    ).toContain("尚未形成可引用");
    expect(
      answerProvenanceLabel(run({ answer_data_freshness: "current_run_evidence" })),
    ).toContain("本次 Run 的已验证数据");
  });
});
