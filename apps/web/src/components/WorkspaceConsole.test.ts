import { createApp, nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Run, RunArtifact, RunEvent, SqlAudit, ToolCall, TraceDag } from "@/api/types";
import type { RunActivity } from "@/lib/runActivity";
import { deriveRunTrace } from "@/lib/runTrace";

import WorkspaceConsole from "./WorkspaceConsole.vue";
import { useArtifactStore } from "@/stores/artifactStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const run: Run = {
  id: "run_1",
  session_id: "session_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    user_message_id: null,
    question: "统计本月销售额",
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
  finished_at: "2026-08-16T00:00:01.500Z",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:01.500Z",
};

const activity: RunActivity = {
  runId: "run_1",
  status: "succeeded",
  completionKind: "completed",
  summary: "分析完成",
  steps: [{
    id: "step:run_1:batch-1",
    runId: "run_1",
    seq: 2,
    title: "第 1 步 · 查询数据",
    detail: "完成 1 个工具，用时 36 ms",
    goal: "确认 orders 的查询结果",
    action: "查询数据",
    result: "返回 12 行",
    status: "succeeded",
    turnNo: null,
    toolCallIds: ["tool_1"],
    items: [{
      id: "tool:tool_1",
      runId: "run_1",
      seq: 2,
      kind: "tool",
      status: "succeeded",
      title: "查询数据",
      detail: "完成，用时 36 ms",
      target: "trace",
      toolCallId: "tool_1",
      artifactId: null,
    }],
  }],
  displayEntries: [{
    kind: "step",
    id: "step:run_1:batch-1",
    seq: 2,
    stepNumber: 1,
    step: {
      id: "step:run_1:batch-1",
      runId: "run_1",
      seq: 2,
      title: "第 1 步 · 查询数据",
      detail: "完成 1 个工具，用时 36 ms",
      goal: "确认 orders 的查询结果",
      action: "查询数据",
      result: "返回 12 行",
      status: "succeeded",
      turnNo: null,
      toolCallIds: ["tool_1"],
      items: [{
        id: "tool:tool_1",
        runId: "run_1",
        seq: 2,
        kind: "tool",
        status: "succeeded",
        title: "查询数据",
        detail: "完成，用时 36 ms",
        target: "trace",
        toolCallId: "tool_1",
        artifactId: null,
      }],
    },
  }],
  toolCount: 1,
  artifactCount: 0,
  auditCount: 1,
  items: [{
    id: "tool:tool_1",
    runId: "run_1",
    seq: 2,
    kind: "tool",
    status: "succeeded",
    title: "查询数据",
    detail: "完成，用时 36 ms",
    target: "trace",
    toolCallId: "tool_1",
    artifactId: null,
  }],
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
  original_sql: "SELECT * FROM orders",
  normalized_sql: "SELECT * FROM orders LIMIT 200",
  status: "succeeded",
  statement_type: "select",
  referenced_tables: ["orders"],
  blocked_reason_code: null,
  blocked_reason: null,
  artifact_id: null,
  row_count: 3,
  elapsed_ms: 36,
  error_code: null,
  error_message: null,
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:01Z",
};

const artifact: RunArtifact = {
  id: "artifact_1",
  run_id: "run_1",
  session_id: "session_1",
  tool_call_id: "tool_1",
  datasource_deleted: false,
  type: "file",
  title: "销售报告.csv",
  mime_type: "text/csv",
  size_bytes: 42,
  inline_previewable: false,
  preview: null,
  metadata: null,
  content_hash: "hash_1",
  created_at: "2026-08-16T00:00:01Z",
};

const toolCall: ToolCall = {
  id: "tool_1",
  run_id: "run_1",
  tool_name: "run_sql_readonly",
  status: "succeeded",
  input_params: { sql: "SELECT * FROM orders" },
  output_summary: { evidence_count: 1 },
  error_code: null,
  error_message: null,
  started_at: "2026-08-16T00:00:00Z",
  finished_at: "2026-08-16T00:00:01Z",
};

const traceEvents: RunEvent[] = [
  { run_id: "run_1", seq: 1, type: "run.started", timestamp: "2026-08-16T00:00:00Z", payload: {} },
  {
    run_id: "run_1",
    seq: 2,
    type: "tool.called",
    timestamp: "2026-08-16T00:00:00.100Z",
    payload: { tool_call_id: "tool_1", tool_name: "run_sql_readonly" },
  },
  {
    run_id: "run_1",
    seq: 3,
    type: "tool.succeeded",
    timestamp: "2026-08-16T00:00:01Z",
    payload: { tool_call_id: "tool_1", tool_name: "run_sql_readonly", elapsed_ms: 36, evidence_count: 1 },
  },
  {
    run_id: "run_1",
    seq: 4,
    type: "artifact.created",
    timestamp: "2026-08-16T00:00:01.100Z",
    payload: { artifact_id: "artifact_1", artifact_type: "file" },
  },
  { run_id: "run_1", seq: 5, type: "run.succeeded", timestamp: "2026-08-16T00:00:01.500Z", payload: {} },
];

const traceEntries = deriveRunTrace({
  run,
  events: traceEvents,
  toolCalls: [toolCall],
  sqlAudits: [audit],
  artifacts: [artifact],
});

const traceDag: TraceDag = {
  run_id: "run_1",
  nodes: [{
    id: "run:run_1:tool:tool_1",
    kind: "tool",
    run_id: "run_1",
    label: "run sql readonly",
    start_seq: 2,
    end_seq: 3,
    status: "succeeded",
    summary: "返回 3 行",
    turn_no: 1,
    tool_call_id: "tool_1",
    artifact_id: null,
    detail_event_seq: 3,
    relationship_status: "resolved",
    action_records: [],
    detail: { tool_name: "run_sql_readonly", evidence_count: 1 },
  }, {
    id: "run:run_1:artifact:artifact_1",
    kind: "artifact",
    run_id: "run_1",
    label: "销售报告.csv",
    start_seq: 4,
    end_seq: 4,
    status: "succeeded",
    summary: "file · text/csv",
    turn_no: null,
    tool_call_id: "tool_1",
    artifact_id: "artifact_1",
    detail_event_seq: 4,
    relationship_status: "resolved",
    action_records: [],
    detail: { artifact_type: "file", size_bytes: 42 },
  }],
  edges: [],
  sections: [],
  warnings: [],
};

async function inspectionDialog(): Promise<HTMLElement> {
  await vi.waitFor(() => expect(document.body.querySelector(".inspection-dialog")).not.toBeNull());
  return document.body.querySelector<HTMLElement>(".inspection-dialog")!;
}

describe("WorkspaceConsole", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("展示当前 Run 摘要，并从调用轨迹打开审计的独立详情", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("trace");
    const selectedDetails: string[] = [];
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [],
      sqlAudits: [audit],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "",
      onSelectDetail: (detailId: string) => selectedDetails.push(detailId),
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.textContent).toContain("开始时间");
    expect(host.textContent).toContain("总耗时");
    expect(host.textContent).toContain("1.5 秒");

    const auditButton = [...host.querySelectorAll<HTMLButtonElement>(".audit-row")][0];
    expect(auditButton).toBeDefined();
    expect(auditButton?.textContent).toContain("SQL 查询 1");
    auditButton?.click();
    await nextTick();

    expect(selectedDetails).toEqual(["audit:audit_1"]);
    expect(workspace.consoleTab).toBe("trace");
    const dialog = await inspectionDialog();
    expect(host.contains(dialog)).toBe(false);
    expect(dialog.textContent).toContain(audit.original_sql);
    app.unmount();
  });

  it("部分完成在概览中使用警告状态，而不是成功状态", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run: { ...run, completion_kind: "partial" },
      activity: { ...activity, completionKind: "partial", summary: "分析部分完成" },
      traceEntries: [],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelector(".console-status.status-partial")?.textContent).toContain("部分完成");
    expect(host.querySelector(".progress-track")).toBeNull();
    expect(host.querySelector(".overview-progress")).toBeNull();
    app.unmount();
  });

  it("已结束的澄清型 Run 不再展示准备项完成比例", async () => {
    const preparation = {
      id: "preparation:requirements",
      runId: "run_1",
      seq: 1,
      kind: "analysis" as const,
      status: "succeeded" as const,
      title: "整理分析目标",
      detail: "已完成",
      target: "overview" as const,
      toolCallId: null,
      artifactId: null,
    };
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run: { ...run, completion_kind: "clarification" },
      activity: {
        ...activity,
        completionKind: "clarification",
        summary: "等待补充分析范围",
        steps: [],
        items: [preparation],
        displayEntries: [{ kind: "item" as const, id: preparation.id, seq: preparation.seq, item: preparation }],
      },
      traceEntries: [],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelector(".overview-progress")).toBeNull();
    expect(host.textContent).not.toContain("1/1 个准备项完成");
    expect(host.textContent).not.toContain("分析进度");
    app.unmount();
  });

  it("概览进度统计与过程卡的统一 displayEntries 保持一致", async () => {
    const preparation = {
      id: "preparation:requirements",
      runId: "run_1",
      seq: 1,
      kind: "analysis" as const,
      status: "succeeded" as const,
      title: "整理分析目标",
      detail: "已完成",
      target: "overview" as const,
      toolCallId: null,
      artifactId: null,
    };
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run,
      activity: {
        ...activity,
        status: "running",
        items: [preparation, ...activity.items],
        displayEntries: [
          { kind: "item", id: preparation.id, seq: preparation.seq, item: preparation },
          ...activity.displayEntries,
        ],
      },
      traceEntries: [],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelectorAll(".console-metrics > div")).toHaveLength(4);
    expect(host.querySelector(".console-metrics")?.textContent).toContain("开始时间");
    expect(host.querySelector(".console-metrics")?.textContent).toContain("工具调用");
    expect(host.textContent).toContain("2/2 项完成");
    app.unmount();
  });

  it("最终答案生成事件不计入未完成的分析过程进度", async () => {
    const answerRequest = {
      id: "final-answer:run_1:6",
      runId: "run_1",
      seq: 6,
      kind: "analysis" as const,
      status: "running" as const,
      title: "开始生成最终答案",
      detail: "第 1 次请求已发出",
      target: "overview" as const,
      toolCallId: null,
      artifactId: null,
    };
    const answerResponse = {
      ...answerRequest,
      id: "final-answer:run_1:7",
      seq: 7,
      status: "succeeded" as const,
      title: "收到最终答案模型回复",
    };
    const answer = {
      ...answerRequest,
      id: "answer:run_1",
      seq: 8,
      kind: "answer" as const,
      status: "succeeded" as const,
      title: "正式答案已生成",
      detail: "可在对话中阅读",
    };
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run,
      activity: {
        ...activity,
        status: "running",
        items: [...activity.items, answerRequest, answerResponse, answer],
        displayEntries: [
          ...activity.displayEntries,
          { kind: "item" as const, id: answerRequest.id, seq: answerRequest.seq, item: answerRequest },
          { kind: "item" as const, id: answerResponse.id, seq: answerResponse.seq, item: answerResponse },
          { kind: "item" as const, id: answer.id, seq: answer.seq, item: answer },
        ],
      },
      traceEntries: [],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.textContent).toContain("1/1 项完成");
    expect(host.textContent).not.toContain("1/4 项完成");
    app.unmount();
  });

  it("产物列表不预加载正文，点击后在 Portal 查看结果并由 Escape 恢复焦点", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    const artifactStore = useArtifactStore();
    const previewableArtifact = { ...artifact, inline_previewable: true };
    const csv = "name,amount\n华东,120\n华南,80\n";
    const blob = new Blob([csv], { type: "text/csv" });
    if (typeof blob.text !== "function") {
      Object.defineProperty(blob, "text", { value: async () => csv });
    }
    artifactStore.contentById = {
      [artifact.id]: {
        blob,
        contentType: "text/csv",
        filename: null,
      },
    };
    workspace.setConsoleTab("outputs");
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [toolCall],
      sqlAudits: [],
      artifacts: [previewableArtifact, { ...previewableArtifact, id: "artifact_2", tool_call_id: "tool_2" }],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    await nextTick();

    expect(host.querySelectorAll(".artifact-row")).toHaveLength(2);
    expect(document.body.querySelector(".artifact-viewer")).toBeNull();
    expect(host.textContent).not.toContain("华东");
    expect(host.querySelector(".artifact-row")?.textContent).toContain("SQL 查询 1");
    expect(host.querySelector(".artifact-row")?.textContent).toContain("42 B");

    const opener = host.querySelector<HTMLButtonElement>(".artifact-row")!;
    opener.focus();
    opener.click();
    const dialog = await inspectionDialog();
    expect(host.contains(dialog)).toBe(false);
    expect(dialog.textContent).toContain("销售报告.csv");
    await vi.waitFor(() => expect(dialog.textContent).toContain("华东"));
    expect(dialog.querySelectorAll(".artifact-viewer")).toHaveLength(1);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await nextTick();
    await vi.waitFor(() => expect(document.body.querySelector(".inspection-dialog")).toBeNull());
    expect(document.activeElement).toBe(opener);
    expect(workspace.consoleTab).toBe("outputs");
    expect(host.querySelector(".console-tabs")).not.toBeNull();
    app.unmount();
  });

  it("将 SQL 审计和表格产物按 tool_call_id 展示在对应调用详情中", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [toolCall],
      sqlAudits: [audit],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "tool:tool_1",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const dialog = await inspectionDialog();
    expect(dialog.textContent).toContain("执行 SQL");
    expect(dialog.textContent).toContain("SELECT * FROM orders");
    expect(dialog.textContent).toContain("涉及表");
    expect(dialog.textContent).toContain("orders");
    expect(dialog.textContent).toContain("销售报告.csv");
    expect(dialog.textContent).toContain("SELECT * FROM orders LIMIT 200");
    app.unmount();
  });

  it("在审计详情中展示原始 SQL、归一化 SQL 和 Guard 阻断原因", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const blockedAudit: SqlAudit = {
      ...audit,
      status: "blocked",
      normalized_sql: null,
      blocked_reason_code: "UNKNOWN_TABLE",
      blocked_reason: "SQL 引用了不在当前 Schema 中的数据表",
    };
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [],
      sqlAudits: [blockedAudit],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "audit:audit_1",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const dialog = await inspectionDialog();
    expect(dialog.textContent).toContain("执行 SQL");
    expect(dialog.textContent).toContain("SQL 查询 1");
    expect(dialog.textContent).toContain("SELECT * FROM orders");
    expect(dialog.textContent).toContain("UNKNOWN_TABLE");
    expect(dialog.textContent).toContain("不在当前 Schema 中");
    expect(dialog.querySelector(".inspection-source header span")?.textContent).toBe("已阻断");
    app.unmount();
  });

  it("按真实 seq 展示原始事件，并从工具事件进入统一详情", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("trace");
    const selectedDetails: string[] = [];
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [toolCall],
      sqlAudits: [audit],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "",
      onSelectDetail: (detailId: string) => selectedDetails.push(detailId),
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const rows = [...host.querySelectorAll<HTMLElement>(".trace-row")];
    expect(rows).toHaveLength(5);
    expect(rows.map((row) => row.querySelector(".trace-index")?.textContent)).toEqual(["#1", "#2", "#3", "#4", "#5"]);
    expect(rows[2]?.querySelector(".trace-action")?.getAttribute("title")).toBe("tool.succeeded");
    expect(host.querySelector<HTMLDetailsElement>(".linear-trace")?.open).toBe(false);
    expect(rows[2]?.textContent).toContain("关联 1 条 SQL 审计");
    rows[2]?.querySelector<HTMLButtonElement>(".trace-action")?.click();
    await nextTick();

    expect(selectedDetails).toEqual(["event:3"]);
    expect(workspace.consoleTab).toBe("trace");
    expect((await inspectionDialog()).textContent).toContain("SQL 查询 1");
    app.unmount();
  });

  it("Trace 同屏展示 DAG 和线性事件，并让 DAG 节点复用 ToolCall 详情", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("trace");
    const selectedDetails: string[] = [];
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      traceDag,
      toolCalls: [toolCall],
      sqlAudits: [audit],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "",
      onSelectDetail: (detailId: string) => selectedDetails.push(detailId),
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    host.querySelector<HTMLInputElement>('input[value="graph"]')?.click();
    await nextTick();
    expect(host.querySelector(".trace-dag-canvas")).not.toBeNull();
    expect(host.querySelectorAll(".trace-row")).toHaveLength(5);
    host.querySelector<SVGGElement>(".trace-node")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();

    expect(selectedDetails).toEqual(["tool:tool_1"]);
    expect(workspace.consoleTab).toBe("trace");
    expect((await inspectionDialog()).textContent).toContain("销售报告.csv");
    app.unmount();
  });

  it("Artifact DAG 节点打开独立产物详情并保留执行过程页签", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("trace");
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      traceDag,
      toolCalls: [toolCall],
      sqlAudits: [],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    host.querySelector<HTMLInputElement>('input[value="graph"]')?.click();
    await nextTick();
    const nodes = host.querySelectorAll<SVGGElement>(".trace-node");
    nodes[1]?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(workspace.consoleTab).toBe("trace");
    expect((await inspectionDialog()).textContent).toContain("销售报告.csv");
    app.unmount();
  });

  it("全屏 Trace 点击普通节点只更新侧栏", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("trace");
    const selectedDetails: string[] = [];
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      traceDag,
      toolCalls: [toolCall],
      sqlAudits: [audit],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "",
      onSelectDetail: (detailId: string) => selectedDetails.push(detailId),
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    host.querySelector<HTMLButtonElement>('button[title="全屏查看"]')?.click();
    await nextTick();
    const dialog = document.body.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();

    dialog?.querySelector<SVGGElement>(".trace-node")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(selectedDetails).toEqual([]);
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.body.querySelector(".trace-node-details")).not.toBeNull();

    expect(document.body.querySelector<HTMLButtonElement>(".trace-node-details-open")).toBeNull();
    expect(selectedDetails).toEqual([]);
    expect(workspace.consoleTab).toBe("trace");
    app.unmount();
  });

  it("全屏 Trace 中 Artifact 通过明确动作打开独立详情", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("trace");
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      traceDag,
      toolCalls: [toolCall],
      sqlAudits: [],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    host.querySelector<HTMLButtonElement>('button[title="全屏查看"]')?.click();
    await nextTick();
    const dialog = document.body.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();

    const nodes = dialog?.querySelectorAll<SVGGElement>(".trace-node");
    nodes?.[1]?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(document.body.querySelector(".trace-node-details-open")).not.toBeNull();
    document.body.querySelector<HTMLButtonElement>(".trace-node-details-open")?.click();
    await nextTick();

    expect(workspace.consoleTab).toBe("trace");
    expect((await inspectionDialog()).textContent).toContain("销售报告.csv");
    expect(document.body.querySelector(".trace-overlay-panel")).toBeNull();
    app.unmount();
  });

  it("工具事件入口展示同一调用的 SQL 和结果", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [toolCall],
      sqlAudits: [audit],
      artifacts: [artifact],
      selectedArtifactId: "",
      selectedDetailId: "event:3",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const dialog = await inspectionDialog();
    expect(dialog.textContent).toContain("SELECT * FROM orders");
    expect(dialog.textContent).toContain("SQL 查询 1");
    expect(dialog.textContent).toContain("销售报告.csv");
    app.unmount();
  });

  it("答案依据将 SQL 和结果合为一组，各入口打开同一独立详情并可重复打开", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const app = createApp(WorkspaceConsole, {
      run, activity, traceEntries, toolCalls: [toolCall], sqlAudits: [audit], artifacts: [artifact],
      answerEvidenceRefs: [audit.id, artifact.id, toolCall.id], selectedArtifactId: "", selectedDetailId: "",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    expect(host.querySelectorAll(".overview-evidence-item")).toHaveLength(1);
    host.querySelector<HTMLButtonElement>(".overview-evidence-item")!.click();
    await nextTick();
    expect((await inspectionDialog()).textContent).toContain("SELECT * FROM orders");
    expect((await inspectionDialog()).textContent).toContain(artifact.title);
    document.body.querySelector<HTMLButtonElement>('button[title="关闭独立详情"]')!.click();
    await nextTick();
    await vi.waitFor(() => expect(document.body.querySelector(".inspection-dialog")).toBeNull());
    expect(workspace.consoleTab).toBe("overview");
    workspace.setConsoleTab("trace");
    await nextTick();
    await vi.waitFor(() => expect(host.querySelector(".audit-row")).not.toBeNull());
    host.querySelector<HTMLButtonElement>(".audit-row")!.click();
    await nextTick();
    expect((await inspectionDialog()).querySelector("h2")?.textContent).toBe("SQL 查询 1");
    document.body.querySelector<HTMLButtonElement>('button[title="关闭独立详情"]')!.click();
    await nextTick();
    await vi.waitFor(() => expect(document.body.querySelector(".inspection-dialog")).toBeNull());
    workspace.setConsoleTab("outputs");
    await nextTick();
    await vi.waitFor(() => expect(host.querySelector(".artifact-row")).not.toBeNull());
    host.querySelector<HTMLButtonElement>(".artifact-row")!.click();
    await nextTick();
    expect((await inspectionDialog()).textContent).toContain("SELECT * FROM orders");
    expect((await inspectionDialog()).textContent).toContain(artifact.title);
    expect(workspace.consoleTab).toBe("outputs");
    app.unmount();
  });

  it("Trace 和事件详情不会混入其他 Run 的审计或产物", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("overview");
    const foreignAudit: SqlAudit = {
      ...audit,
      id: "audit_other",
      run_id: "run_other",
      tool_call_id: "tool_1",
      original_sql: "SELECT secret FROM other_run",
    };
    const foreignArtifact: RunArtifact = {
      ...artifact,
      id: "artifact_other",
      run_id: "run_other",
      title: "其他 Run 的产物",
    };
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries,
      toolCalls: [toolCall],
      sqlAudits: [audit, foreignAudit],
      artifacts: [artifact, foreignArtifact],
      selectedArtifactId: "",
      selectedDetailId: "event:3",
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const dialog = await inspectionDialog();
    expect(dialog.textContent).toContain("SELECT * FROM orders");
    expect(dialog.textContent).toContain("销售报告.csv");
    expect(dialog.textContent).not.toContain("SELECT secret FROM other_run");
    expect(dialog.textContent).not.toContain("其他 Run 的产物");
    app.unmount();
  });

  it("Run 页签列出当前会话紧凑目录，点选后交给 selectRun", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspace = useWorkspaceStore();
    const sessionStore = useSessionStore();
    sessionStore.runs = [run];
    workspace.setConsoleTab("runs");
    const selectedRuns: string[] = [];
    const app = createApp(WorkspaceConsole, {
      run,
      activity,
      traceEntries: [],
      toolCalls: [],
      sqlAudits: [],
      artifacts: [],
      selectedArtifactId: "",
      selectedDetailId: "",
      onSelectRun: (runId: string) => selectedRuns.push(runId),
    });
    app.use(pinia);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelector(".run-catalog-all")?.textContent).toContain("查看全部");
    expect(host.textContent).toContain(run.question);
    host.querySelector<HTMLButtonElement>(".run-catalog-row")?.click();
    await nextTick();
    expect(selectedRuns).toEqual(["run_1"]);
    app.unmount();
  });
});
