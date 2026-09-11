import { describe, expect, it } from "vitest";

import type { Run, RunArtifact, RunEvent, SqlAudit, ToolCall } from "@/api/types";

import { deriveRunActivity } from "./runActivity";

function event(seq: number, type: RunEvent["type"], payload: RunEvent["payload"] = {}, runId = "run_1"): RunEvent {
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
  return { run_id: runId, seq, type, timestamp: "2026-08-16T00:00:00Z", payload: canonicalPayload };
}

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: "run_1",
    session_id: "session_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    user_message_id: null,
    question: "按月统计销售额",
    status: "running",
    protocol_id: "data-analysis",
    model_profile_id: "model_1",
    model_provider: "openai",
    model_name: "test",
    schema_revision: 1,
    datalink_graph_version: null,
    run_timeout_seconds: 120,
    completion_kind: null,
    incomplete_reason: null,
    error_code: null,
    error_message: null,
    cancel_requested_at: null,
    cancel_reason: null,
    started_at: "2026-08-16T00:00:00Z",
    finished_at: null,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:00Z",
    ...overrides,
  };
}

function toolCall(overrides: Partial<ToolCall> = {}): ToolCall {
  return {
    id: "tool_1",
    run_id: "run_1",
    tool_name: "run_sql_readonly",
    status: "succeeded",
    input_params: null,
    output_summary: null,
    error_code: null,
    error_message: null,
    started_at: "2026-08-16T00:00:00Z",
    finished_at: "2026-08-16T00:00:01Z",
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
    title: "月度销售额",
    mime_type: "application/json",
    size_bytes: 120,
    inline_previewable: true,
    preview: null,
    metadata: null,
    content_hash: "hash_1",
    created_at: "2026-08-16T00:00:01Z",
    ...overrides,
  };
}

const noAudits: SqlAudit[] = [];

describe("deriveRunActivity", () => {
  it("按 Run 和持久化标识分组工具与产物，并用审计数量补充 SQL 活动", () => {
    const activity = deriveRunActivity({
      run: run(),
      events: [
        event(1, "run.started"),
        event(2, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
        event(3, "artifact.created", { artifact_id: "artifact_1", artifact_type: "table" }),
        event(4, "tool.succeeded", {
          tool_call_id: "tool_1",
          tool_name: "run_sql_readonly",
          elapsed_ms: 36,
          evidence_count: 1,
        }),
        event(5, "tool.called", { tool_call_id: "other_tool", tool_name: "run_python" }, "run_other"),
      ],
      toolCalls: [toolCall(), toolCall({ id: "other_tool", run_id: "run_other", tool_name: "run_python" })],
      sqlAudits: [
        {
          id: "audit_1",
          run_id: "run_1",
          tool_call_id: "tool_1",
          datasource_id: "datasource_1",
          datasource_deleted: false,
          schema_revision: 1,
          attempt_no: 1,
          repaired_from_id: null,
          original_sql: "SELECT * FROM orders",
          normalized_sql: "SELECT * FROM orders LIMIT 200",
          status: "succeeded",
          statement_type: "select",
          referenced_tables: ["orders"],
          blocked_reason_code: null,
          blocked_reason: null,
          artifact_id: "artifact_1",
          row_count: 12,
          elapsed_ms: 36,
          error_code: null,
          error_message: null,
          created_at: "2026-08-16T00:00:00Z",
          updated_at: "2026-08-16T00:00:01Z",
        },
      ],
      artifacts: [artifact(), artifact({ id: "artifact_2", run_id: "run_other" })],
    });

    expect(activity.items.map((item) => item.id)).toEqual([
      "analysis:run_1",
      "tool:tool_1",
      "artifact:artifact_1",
    ]);
    expect(activity.items[1]).toMatchObject({
      seq: 2,
      title: "查询数据",
      status: "succeeded",
      detail: "完成，用时 36 ms",
      target: "trace",
      toolCallId: "tool_1",
    });
    expect(activity.items[2]).toMatchObject({ artifactId: "artifact_1", title: "月度销售额" });
    expect(activity.steps).toHaveLength(1);
    expect(activity.steps[0]).toMatchObject({
      title: "第 1 步 · 查询数据",
      goal: "确认 orders 的查询结果",
      action: "查询数据",
      result: "返回 12 行；涉及 orders；耗时 36 ms；审计通过",
      status: "succeeded",
      toolCallIds: ["tool_1"],
    });
    expect(activity).toMatchObject({ toolCount: 1, artifactCount: 1, auditCount: 1 });
  });

  it("将同一分析回合中的多个工具调用聚合为一个步骤", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        event(2, "agent.turn.started", { turn_no: 1 }),
        event(3, "agent.turn.completed", { turn_no: 1 }),
        event(4, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
        event(5, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "run_sql_readonly", elapsed_ms: 10, evidence_count: 1 }),
        event(6, "tool.called", { tool_call_id: "tool_2", tool_name: "run_python" }),
        event(7, "tool.succeeded", { tool_call_id: "tool_2", tool_name: "run_python", elapsed_ms: 20, evidence_count: 1 }),
      ],
      toolCalls: [
        toolCall(),
        toolCall({ id: "tool_2", tool_name: "run_python" }),
      ],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.steps).toHaveLength(1);
    expect(activity.steps[0]).toMatchObject({
      turnNo: 1,
      toolCallIds: ["tool_1", "tool_2"],
      status: "succeeded",
      detail: "已决定 2 个工具动作；完成，用时 30 ms",
    });
  });

  it("把 Agent 回合摘要作为父步骤，并按真实 seq 挂载工具结果", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        event(2, "agent.turn.started", { turn_no: 1 }),
        event(3, "agent.turn.completed", {
          turn_no: 1,
          elapsed_ms: 18,
          status: "completed",
          action_kind: "tool_call",
          tool_names: "run_sql_readonly",
          tool_call_count: 1,
        }),
        event(4, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
        event(5, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "run_sql_readonly", elapsed_ms: 10 }),
      ],
      toolCalls: [toolCall()],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.displayEntries[0]).toMatchObject({ kind: "step", seq: 2 });
    expect(activity.steps[0]).toMatchObject({
      title: "第 1 轮 Agent 分析",
      action: "调用查询数据",
      actionKind: "tool_call",
      toolNames: ["run_sql_readonly"],
      toolCallIds: ["tool_1"],
    });
    expect(activity.items.some((item) => item.id === "analysis:run_1")).toBe(false);
  });

  it("展示结论提交工具及其事实证据数量", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        event(2, "tool.called", { tool_call_id: "commit_1", tool_name: "commit_analysis_claims" }),
        event(3, "tool.succeeded", {
          tool_call_id: "commit_1",
          tool_name: "commit_analysis_claims",
          elapsed_ms: 8,
          evidence_count: 2,
          output_summary_json: JSON.stringify({ claim_count: 1, evidence_binding_count: 2 }),
        }),
      ],
      toolCalls: [toolCall({
        id: "commit_1",
        tool_name: "commit_analysis_claims",
        output_summary: { claim_count: 1, evidence_binding_count: 2 },
      })],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.items[1]).toMatchObject({ title: "提交分析结论", status: "succeeded" });
    expect(activity.steps[0]).toMatchObject({
      action: "提交分析结论",
      result: "提交 1 个结论，绑定 2 条事实证据",
    });
  });

  it("从 DataLink 的语义投影生成字段、实体和物理关系摘要", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        event(2, "tool.called", { tool_call_id: "tool_1", tool_name: "explore_datalink" }),
        event(3, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "explore_datalink", elapsed_ms: 8, evidence_count: 1 }),
      ],
      toolCalls: [toolCall({
        tool_name: "explore_datalink",
        input_params: { query: "查看所有表及其关联关系", focus: "join_paths", max_nodes: 20 },
        output_summary: {
          node_count: 5,
          edge_count: 10,
          semantic_entity_count: 2,
          semantic_mapping_count: 6,
        },
      })],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.steps[0]).toMatchObject({
      goal: "确认数据表之间的关联",
      action: "查看数据关系",
      result: "找到 5 个字段，关联 2 个语义实体、6 条字段实体关联、10 条物理关系",
    });
  });

  it("展示 Python 退出码和产物数量，不把脚本正文放进过程摘要", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        event(2, "tool.called", { tool_call_id: "python_1", tool_name: "run_python" }),
        event(3, "tool.succeeded", { tool_call_id: "python_1", tool_name: "run_python", elapsed_ms: 42 }),
      ],
      toolCalls: [toolCall({
        id: "python_1",
        tool_name: "run_python",
        input_params: { purpose: "生成趋势图", script: "print('secret script')" },
        output_summary: { exit_code: 0, output_count: 1, stdout: "内部输出不应进入摘要" },
      })],
      sqlAudits: noAudits,
      artifacts: [artifact({ id: "chart_1", tool_call_id: "python_1", type: "chart", title: "趋势图" })],
    });

    expect(activity.steps[0]?.result).toBe("退出码 0；生成 1 个产物");
    expect(JSON.stringify(activity)).not.toContain("secret script");
    expect(JSON.stringify(activity)).not.toContain("内部输出不应进入摘要");
  });

  it("缺少 turn_no 的工具事件不会被猜测分组", () => {
    const tool1Called = event(2, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" });
    const tool1Succeeded = event(3, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "run_sql_readonly", elapsed_ms: 5, evidence_count: 1 });
    const tool2Called = event(5, "tool.called", { tool_call_id: "tool_2", tool_name: "run_python" });
    const tool2Succeeded = event(6, "tool.succeeded", { tool_call_id: "tool_2", tool_name: "run_python", elapsed_ms: 7, evidence_count: 1 });
    delete tool1Called.payload.turn_no;
    delete tool1Succeeded.payload.turn_no;
    delete tool2Called.payload.turn_no;
    delete tool2Succeeded.payload.turn_no;
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        tool1Called,
        tool1Succeeded,
        event(4, "answer.delta", { delta: "回答" }),
        tool2Called,
        tool2Succeeded,
      ],
      toolCalls: [
        toolCall(),
        toolCall({ id: "tool_2", tool_name: "run_python" }),
      ],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.steps).toHaveLength(0);
  });

  it("将连续 answer.delta 压缩为一条整理答案活动，不展示草稿正文", () => {
    const activity = deriveRunActivity({
      run: run(),
      events: [
        event(1, "run.started"),
        event(2, "answer.delta", { delta: "不展示" }),
        event(3, "answer.delta", { delta: "模型草稿" }),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.summary).toBe("正在整理答案");
    expect(activity.items.filter((item) => item.kind === "answer")).toEqual([
      expect.objectContaining({ id: "answer:run_1", seq: 2, title: "正在整理答案", status: "running" }),
    ]);
    expect(JSON.stringify(activity)).not.toContain("模型草稿");
  });

  it("在工具尚未结束时显示当前安全的运行状态", () => {
    const activity = deriveRunActivity({
      run: run(),
      events: [
        event(1, "run.started"),
        event(2, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.summary).toBe("正在查询数据");
    expect(activity.items.find((item) => item.toolCallId === "tool_1")).toMatchObject({
      status: "running",
      target: "trace",
    });
  });

  it("展示持久化准备阶段，不把数据地图正文带入活动卡", () => {
    const activity = deriveRunActivity({
      run: run(),
      events: [
        event(1, "run.started"),
        event(2, "run.preparation.started", { phase: "run_opening" }),
        event(3, "run.preparation.completed", { phase: "run_opening", elapsed_ms: 12 }),
        event(4, "run.preparation.started", {
          phase: "semantic_context",
          semantic_context: "must-not-reach-activity",
        }),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.summary).toBe("正在读取数据地图");
    expect(activity.items.filter((item) => item.kind === "analysis")).toEqual([
      expect.objectContaining({ title: "判断本次处理方式", status: "succeeded", detail: "已完成，用时 12 ms；暂无可展示的结构化结果" }),
      expect.objectContaining({ title: "读取数据地图", status: "running", detail: "正在处理" }),
    ]);
    expect(JSON.stringify(activity)).not.toContain("must-not-reach-activity");
  });

  it("按 seq 交错展示准备阶段、工具步骤和最终答案", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded" }),
      events: [
        event(1, "run.started"),
        event(2, "run.preparation.started", { phase: "run_opening" }),
        event(3, "run.preparation.completed", { phase: "run_opening", elapsed_ms: 12 }),
        event(4, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
        event(5, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "run_sql_readonly", elapsed_ms: 10 }),
        event(6, "answer.ready"),
      ],
      toolCalls: [toolCall()],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.displayEntries.map((entry) => entry.kind === "step" ? entry.step.title : entry.item.title)).toEqual([
      "判断本次处理方式",
      "第 1 步 · 查询数据",
      "正式答案已生成",
    ]);
    expect(activity.displayEntries.map((entry) => entry.seq)).toEqual([2, 4, 6]);
  });

  it("准备阶段失败后继续运行时仍保留失败状态", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded", completion_kind: "partial" }),
      events: [
        event(1, "run.started"),
        event(2, "run.preparation.started", { phase: "analysis_plan" }),
        event(3, "run.preparation.completed", {
          phase: "analysis_plan",
          elapsed_ms: 1_200,
          status: "timed_out",
          failure_code: "ANALYSIS_PREPARATION_TIMEOUT",
        }),
        event(4, "tool.called", { tool_call_id: "tool_1", tool_name: "run_sql_readonly" }),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.items).toEqual(expect.arrayContaining([
      expect.objectContaining({
        title: "校验分析计划",
        status: "failed",
        detail: "未完成：ANALYSIS_PREPARATION_TIMEOUT",
      }),
    ]));
  });

  it("终态优先呈现完成结果，并保留正式答案活动", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded", completion_kind: "partial", finished_at: "2026-08-16T00:02:00Z" }),
      events: [event(1, "run.started"), event(2, "answer.ready", { assistant_message_id: "message_1", artifact_count: 0, completion_kind: "partial", incomplete_reason: "RUN_TIMEOUT" }), event(3, "run.succeeded")],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.summary).toBe("分析部分完成");
    expect(activity.items.at(-1)).toMatchObject({ kind: "answer", status: "succeeded", title: "正式答案已生成" });
  });

  it("澄清型 Run 将正式 Assistant 消息显示为补充范围请求", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded", completion_kind: "clarification" }),
      events: [
        event(1, "run.started"),
        event(2, "run.preparation.started", { phase: "run_opening" }),
        event(3, "run.preparation.completed", { phase: "run_opening", elapsed_ms: 12 }),
        event(4, "answer.ready", { completion_kind: "clarification" }),
        event(5, "run.succeeded"),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.summary).toBe("等待补充分析范围");
    expect(activity.items.at(-1)).toMatchObject({
      kind: "answer",
      status: "succeeded",
      title: "已请求补充分析范围",
      detail: "等待补充后继续分析",
    });
    expect(activity.items.at(-1)?.title).not.toBe("正式答案已生成");
  });

  it("保留最终答案请求和校验失败的安全过程", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded", completion_kind: "partial" }),
      events: [
        event(1, "run.started"),
        event(2, "final_answer.request.started", { attempt: 1, mode: "json_schema" }),
        event(3, "final_answer.response.received", { attempt: 1, mode: "json_schema", elapsed_ms: 245 }),
        event(4, "final_answer.validation.failed", {
          attempt: 1,
          mode: "json_schema",
          elapsed_ms: 245,
          failure_code: "MODEL_OUTPUT_INVALID",
          validation_stage: "markdown",
        }),
        event(5, "answer.ready", {
          assistant_message_id: "message_1",
          completion_kind: "partial",
          incomplete_reason: "FINAL_ANSWER_INVALID",
        }),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.items.filter((item) => item.id.startsWith("final-answer:"))).toEqual([
      expect.objectContaining({
        id: "final-answer:run_1:1",
        seq: 2,
        title: "最终答案未通过校验",
        status: "failed",
        detail: "结构化答案已收到，但 Markdown 块结构不合规，原文不会展示",
      }),
    ]);
    expect(activity.items.some((item) => item.status === "running")).toBe(false);
    expect(activity.items.at(-1)).toMatchObject({
      kind: "answer",
      status: "succeeded",
      title: "正式答案已生成",
    });
    expect(JSON.stringify(activity)).not.toContain("候选正文");
  });

  it("最终答案请求进行中时活动卡保持进行中", () => {
    const activity = deriveRunActivity({
      run: run({ status: "running" }),
      events: [
        event(1, "run.started"),
        event(2, "final_answer.request.started", { attempt: 1, mode: "json_schema" }),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.items.filter((item) => item.id.startsWith("final-answer:"))).toEqual([
      expect.objectContaining({
        id: "final-answer:run_1:1",
        status: "running",
        title: "开始生成最终答案",
        detail: "第 1 次请求已发出",
      }),
    ]);
  });

  it("已完成 Run 将同一次最终答案请求合并为成功项，不再残留进行中", () => {
    const activity = deriveRunActivity({
      run: run({ status: "succeeded", completion_kind: "completed" }),
      events: [
        event(1, "run.started"),
        event(2, "final_answer.request.started", { attempt: 1, mode: "json_schema" }),
        event(3, "final_answer.response.received", { attempt: 1, mode: "json_schema", elapsed_ms: 180 }),
        event(4, "answer.ready"),
        event(5, "run.succeeded"),
      ],
      toolCalls: [],
      sqlAudits: noAudits,
      artifacts: [],
    });

    expect(activity.items.filter((item) => item.id.startsWith("final-answer:"))).toEqual([
      expect.objectContaining({
        id: "final-answer:run_1:1",
        seq: 2,
        status: "succeeded",
        title: "收到最终答案模型回复",
        detail: "回复已收到，用时 180 ms",
      }),
    ]);
    expect(activity.items.some((item) => item.status === "running")).toBe(false);
    expect(activity.items.at(-1)).toMatchObject({
      kind: "answer",
      status: "succeeded",
      title: "正式答案已生成",
    });
  });
});
