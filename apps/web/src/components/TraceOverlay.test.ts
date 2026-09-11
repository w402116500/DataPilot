import { createApp, h, nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TraceDag, TraceDagNode } from "@/api/types";
import type { RunTraceEntry } from "@/lib/runTrace";

import TraceOverlay from "./TraceOverlay.vue";

const toolNode: TraceDagNode = {
  id: "run:run_1:tool:tool_1",
  kind: "tool",
  run_id: "run_1",
  label: "run sql readonly",
  start_seq: 2,
  end_seq: 3,
  status: "succeeded",
  summary: "row count: 3",
  turn_no: 1,
  tool_call_id: "tool_1",
  artifact_id: null,
  detail_event_seq: 3,
  relationship_status: "resolved",
  action_records: [{
    id: "run:run_1:action:2:tool.called",
    kind: "tool_requested",
    event_seq: 2,
    status: "running",
    label: "run_sql_readonly requested",
    summary: null,
    tool_call_id: "tool_1",
    artifact_id: null,
    reason: null,
  }, {
    id: "run:run_1:action:3:tool.succeeded",
    kind: "tool_completed",
    event_seq: 3,
    status: "succeeded",
    label: "run_sql_readonly succeeded",
    summary: "row count: 3",
    tool_call_id: "tool_1",
    artifact_id: null,
    reason: null,
  }],
  detail: { tool_name: "run_sql_readonly", row_count: 3 },
};

const dag: TraceDag = {
  run_id: "run_1",
  nodes: [toolNode],
  edges: [],
  sections: [],
  warnings: [],
};

const agentNode: TraceDagNode = {
  id: "run:run_1:turn:1",
  kind: "agent-turn",
  run_id: "run_1",
  label: "Agent turn 1",
  start_seq: 1,
  end_seq: 2,
  status: "completed",
  summary: "tool call: run_sql_readonly",
  turn_no: 1,
  tool_call_id: null,
  artifact_id: null,
  detail_event_seq: 2,
  relationship_status: "resolved",
  action_records: [],
  detail: {
    turn_no: 1,
    reasoning: "先检查指标定义，再请求 SQL。",
    assistant_output: "我将先核对数据范围。",
  },
};

describe("TraceOverlay", () => {
  afterEach(() => document.body.replaceChildren());

  it("shows the selected stage's members and links member selection back to the graph", async () => {
    const selectedId = ref("");
    const grouped: TraceDag = {
      ...dag,
      nodes: [agentNode, toolNode],
      sections: [{ id: "section:1", title: "Agent turn 1", start_seq: 1, end_seq: 3, status: "failed", node_ids: [agentNode.id, toolNode.id] }],
    };
    const app = createApp({ render: () => h(TraceOverlay, {
      open: true, dag: grouped, selectedNodeId: selectedId.value,
      onSelectNode: (node: TraceDagNode) => { selectedId.value = node.id; },
    }) });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    document.body.querySelector<SVGGElement>(".trace-node")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(selectedId.value).toBe("");
    expect(document.body.querySelector(".trace-section-details")?.textContent).toContain("未完成");
    expect(document.body.querySelectorAll(".trace-section-details button")).toHaveLength(2);
    document.body.querySelectorAll<HTMLButtonElement>(".trace-section-details button")[1]!.click();
    await nextTick();
    expect(selectedId.value).toBe(toolNode.id);
    expect(document.body.querySelector(".trace-section-details")).toBeNull();
    expect(document.body.querySelector(".trace-node-details h3")?.textContent).toBe("查询数据");
    expect(document.body.querySelectorAll(".trace-node.selected")).toHaveLength(1);
    expect(document.body.querySelector(".trace-section-chip.active")?.getAttribute("aria-expanded")).toBe("true");
    app.unmount();
  });

  it("counts persisted tool calls instead of their individual events", async () => {
    const app = createApp(TraceOverlay, {
      open: true,
      dag,
      toolCalls: [{
        id: "tool_1", run_id: "run_1", tool_name: "run_sql_readonly",
        status: "succeeded", input_params: null, output_summary: null,
        error_code: null, error_message: null,
        started_at: "2026-09-05T00:00:00Z", finished_at: "2026-09-05T00:00:01Z",
      }],
      traceEntries: ["tool.called", "tool.succeeded"].map((eventType, index) => ({
        id: String(index), detailId: "tool_1", seq: index + 1,
        eventType: eventType as "tool.called" | "tool.succeeded",
        timestamp: "2026-09-05T00:00:00Z", title: "查询数据", summary: "",
        status: "succeeded" as const, statusLabel: "已完成", toolCallId: "tool_1",
        artifactId: null, resultSummary: null,
      })),
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    expect(document.body.querySelector("footer")?.textContent).toContain("1 次工具调用");
    expect(document.body.querySelector("footer")?.textContent).not.toContain("2 次工具调用");
    app.unmount();
  });

  it("starts with event details collapsed and presents one navigable row for consecutive answer fragments", async () => {
    const traceEntries: RunTraceEntry[] = [10, 11, 12, 13].map((seq) => ({
      id: `trace:run_1:${seq}`, detailId: `event:${seq}`, seq,
      eventType: seq === 13 ? "answer.ready" : "answer.delta",
      timestamp: "2026-09-05T00:00:00Z",
      title: seq === 13 ? "最终答案已就绪" : "生成答案内容", summary: "已记录",
      status: "succeeded", statusLabel: "已记录", toolCallId: null,
      artifactId: null, resultSummary: null,
    }));
    const selected: RunTraceEntry[] = [];
    const app = createApp(TraceOverlay, { open: true, dag, traceEntries, onSelectEntry: (entry: RunTraceEntry) => selected.push(entry) });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const details = document.body.querySelector<HTMLDetailsElement>(".trace-overlay-list")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")?.textContent).toContain("2 项");
    details.querySelector("summary")!.click();
    expect(details.open).toBe(true);
    const rows = details.querySelectorAll(".trace-overlay-entries li");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("累计 3 个答案片段");
    expect(rows[0]?.textContent).toContain("#10-12");
    expect(rows[1]?.textContent).toContain("最终答案已就绪");
    rows[0]?.querySelector<HTMLButtonElement>("button")!.click();
    expect(selected).toHaveLength(1);
    expect(selected[0]).toMatchObject({ id: "trace:run_1:10", detailId: "event:10", seq: 10 });
    expect(document.body.querySelector(".trace-overlay-footer")?.textContent).toContain("4 条记录");
    app.unmount();
  });

  it("keeps ordinary node selection inside the overlay without an outer navigation action", async () => {
    const selected: TraceDagNode[] = [];
    const opened: TraceDagNode[] = [];
    const app = createApp(TraceOverlay, {
      open: true,
      dag,
      selectedNodeId: toolNode.id,
      onSelectNode: (node: TraceDagNode) => selected.push(node),
      onOpenNode: (node: TraceDagNode) => opened.push(node),
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(document.body.querySelector(".trace-overlay-body")).not.toBeNull();
    expect(document.body.querySelector(".trace-node-details")?.textContent).toContain("row count: 3");
    expect(document.body.querySelector(".trace-node-details")?.textContent).not.toContain("未记录可验证原因");
    expect(document.body.querySelector(".trace-node-details-action-reason")).toBeNull();
    document.body.querySelector<SVGGElement>(".trace-node")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(selected).toHaveLength(1);
    expect(opened).toHaveLength(0);
    expect(document.body.querySelector(".trace-overlay-body")).not.toBeNull();

    expect(document.body.querySelector<HTMLButtonElement>(".trace-node-details-open")).toBeNull();
    expect(opened).toHaveLength(0);
    app.unmount();
  });

  it("shows a warning for a failed action without a persisted reason", async () => {
    const failedNode: TraceDagNode = {
      ...toolNode,
      status: "failed",
      action_records: [{
        ...toolNode.action_records[1]!,
        status: "failed",
        reason: null,
      }],
    };
    const app = createApp(TraceOverlay, {
      open: true,
      dag: { ...dag, nodes: [failedNode] },
      selectedNodeId: failedNode.id,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(document.body.textContent).toContain("失败原因未提供");
    expect(document.body.querySelector(".trace-node-details-action-reason-missing")).not.toBeNull();
    app.unmount();
  });

  it("opens an artifact only through the explicit details action", async () => {
    const artifactNode: TraceDagNode = {
      ...toolNode,
      id: "run:run_1:artifact:artifact_1",
      kind: "artifact",
      artifact_id: "artifact_1",
      action_records: [],
    };
    const selected: TraceDagNode[] = [];
    const opened: TraceDagNode[] = [];
    const app = createApp(TraceOverlay, {
      open: true,
      dag: { ...dag, nodes: [artifactNode] },
      selectedNodeId: artifactNode.id,
      onSelectNode: (node: TraceDagNode) => selected.push(node),
      onOpenNode: (node: TraceDagNode) => opened.push(node),
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    document.body.querySelector(".trace-node")?.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(selected).toHaveLength(1);
    expect(selected[0]).toMatchObject(artifactNode);
    expect(opened).toEqual([]);
    document.body.querySelector<HTMLButtonElement>(".trace-node-details-open")?.click();
    expect(opened).toEqual([artifactNode]);
    app.unmount();
  });

  it.each(["Escape", "close button", "backdrop"])("closes from %s and restores focus without changing node selection", async (action) => {
    const updates: boolean[] = [];
    const selections: TraceDagNode[] = [];
    const open = ref(false);
    const app = createApp({
      render: () => h(TraceOverlay, {
        open: open.value,
        dag,
        selectedNodeId: toolNode.id,
        "onUpdate:open": (value: boolean) => {
          updates.push(value);
          open.value = value;
        },
        onSelectNode: (node: TraceDagNode) => selections.push(node),
      }),
    });
    const host = document.createElement("div");
    const opener = document.createElement("button");
    opener.textContent = "全屏查看";
    document.body.append(opener, host);
    app.mount(host);
    await nextTick();
    opener.focus();
    open.value = true;
    await vi.waitFor(() => expect(document.activeElement?.getAttribute("title")).toBe("关闭关系图"));

    if (action === "Escape") {
      document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    } else if (action === "close button") {
      document.body.querySelector<HTMLButtonElement>('[title="关闭关系图"]')?.click();
    } else {
      document.body.querySelector(".trace-overlay-backdrop")?.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    }

    await vi.waitFor(() => expect(document.body.querySelector('[role="dialog"]')).toBeNull());
    expect(updates).toEqual([false]);
    expect(selections).toEqual([]);
    expect(document.activeElement).toBe(opener);
    app.unmount();
  });

  it("portals outside filtered parents and keeps keyboard focus inside the fullscreen dialog", async () => {
    const host = document.createElement("div");
    host.style.backdropFilter = "blur(20px)";
    host.style.overflow = "hidden";
    const outsideButton = document.createElement("button");
    document.body.append(outsideButton, host);
    const app = createApp(TraceOverlay, { open: true, dag: null });
    app.mount(host);
    await vi.waitFor(() => expect(document.activeElement?.getAttribute("title")).toBe("关闭关系图"));

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!;
    const closeButton = dialog.querySelector<HTMLButtonElement>('[title="关闭关系图"]')!;
    const lastButton = dialog.querySelector<HTMLButtonElement>('[title="放大"]')!;
    expect(host.contains(dialog)).toBe(false);
    expect(document.getElementById(dialog.getAttribute("aria-labelledby")!)?.textContent).toBe("执行关系图");

    closeButton.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(lastButton);
    lastButton.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(closeButton);
    outsideButton.focus();
    expect(document.activeElement).toBe(closeButton);
    app.unmount();
  });

  it("shows the persisted Agent reasoning and output in the side inspector", async () => {
    const agentDag: TraceDag = { ...dag, nodes: [agentNode] };
    const app = createApp(TraceOverlay, {
      open: true,
      dag: agentDag,
      selectedNodeId: agentNode.id,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(document.body.textContent).toContain("先检查指标定义，再请求 SQL。");
    expect(document.body.textContent).toContain("我将先核对数据范围。");
    app.unmount();
  });
});
