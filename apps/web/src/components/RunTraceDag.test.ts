import { createApp, h, nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TraceDag } from "@/api/types";

import RunTraceDag from "./RunTraceDag.vue";

const dag: TraceDag = {
  run_id: "run_1",
  nodes: [{
    id: "run:run_1:turn:1",
    kind: "agent-turn",
    run_id: "run_1",
    label: "Agent turn 1",
    start_seq: 2,
    end_seq: 3,
    status: "completed",
    summary: "tool call: run_sql_readonly",
    turn_no: 1,
    tool_call_id: null,
    artifact_id: null,
    detail_event_seq: 2,
    relationship_status: "resolved",
    action_records: [{
      id: "run_1:2:turn_started",
      kind: "turn_started",
      event_seq: 2,
      status: "running",
      label: "Agent turn 1 started",
      summary: "tool call",
      tool_call_id: null,
      artifact_id: null,
      reason: null,
    }, {
      id: "run_1:3:turn_completed",
      kind: "turn_completed",
      event_seq: 3,
      status: "completed",
      label: "Agent turn 1 completed",
      summary: "2 tools completed",
      tool_call_id: null,
      artifact_id: null,
      reason: null,
    }],
    detail: { turn_no: 1, tool_call_count: 2 },
  }],
  edges: [],
  sections: [{
    id: "run:run_1:section:turn:1",
    title: "Agent turn 1",
    start_seq: 2,
    end_seq: 3,
    status: "completed",
    node_ids: ["run:run_1:turn:1"],
  }],
  warnings: [],
};

function chainDag(count: number): TraceDag {
  const nodes = Array.from({ length: count }, (_, index) => ({
    ...dag.nodes[0]!,
    id: `node:${index}`,
    label: `Node ${index}`,
    start_seq: index + 1,
    end_seq: index + 1,
  }));
  return {
    ...dag,
    nodes,
    edges: nodes.slice(1).map((node, index) => ({
      id: `edge:${index}`,
      source: nodes[index]!.id,
      target: node.id,
      kind: "continues",
      label: null,
    })),
    sections: [],
  };
}

function cameraTransform(host: HTMLElement): { x: number; y: number; scale: number } {
  const transform = host.querySelector(".trace-dag-canvas > g")?.getAttribute("transform") ?? "";
  const values = [...transform.matchAll(/[-+]?\d*\.?\d+(?:e[-+]?\d+)?/gi)].map((match) => Number(match[0]));
  expect(values).toHaveLength(3);
  return { x: values[0]!, y: values[1]!, scale: values[2]! };
}

function nodePositions(host: HTMLElement): { x: number; y: number }[] {
  return [...host.querySelectorAll(".trace-node")].map((node) => {
    const values = [...node.getAttribute("transform")!.matchAll(/[-+]?\d*\.?\d+(?:e[-+]?\d+)?/gi)].map((match) => Number(match[0]));
    expect(values).toHaveLength(2);
    return { x: values[0]!, y: values[1]! };
  });
}

describe("RunTraceDag", () => {
  it("links group expansion, status, highlighting and externally selected hidden members", async () => {
    const member = { ...dag.nodes[0]!, id: "tool:member", kind: "tool" as const, label: "查询数据", status: "failed" };
    const grouped: TraceDag = { ...dag, nodes: [...dag.nodes, member], sections: [{ ...dag.sections[0]!, status: "failed", node_ids: [dag.nodes[0]!.id, member.id] }] };
    const selectedId = ref("");
    const selectNode = vi.fn();
    const selectSection = vi.fn();
    const app = createApp({ render: () => h(RunTraceDag, { dag: grouped, mode: "overlay", selectedNodeId: selectedId.value, onSelectNode: selectNode, onSelectSection: selectSection }) });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    const collapsed = host.querySelector<SVGGElement>(".trace-node")!;
    expect(collapsed.getAttribute("aria-label")).toContain("未完成");
    collapsed.dispatchEvent(new MouseEvent("pointerdown", { button: 0, bubbles: true }));
    collapsed.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(selectNode).not.toHaveBeenCalled();
    expect(selectSection).toHaveBeenCalledOnce();
    expect(host.querySelectorAll(".trace-node.selected")).toHaveLength(2);
    const chip = host.querySelector<HTMLButtonElement>(".trace-section-chip")!;
    expect(chip.getAttribute("aria-expanded")).toBe("true");
    chip.click();
    await nextTick();
    expect(host.querySelectorAll(".trace-node")).toHaveLength(1);
    selectedId.value = member.id;
    await nextTick();
    expect(host.querySelectorAll(".trace-node")).toHaveLength(2);
    expect(host.querySelectorAll(".trace-node.selected")).toHaveLength(1);
    expect(host.querySelector(".trace-node.selected")?.textContent).toContain("查询数据");
    expect(chip.getAttribute("aria-expanded")).toBe("true");
    app.unmount();
  });
  afterEach(() => {
    document.body.replaceChildren();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("defaults to readable stages, preserves membership and custom labels, and switches views locally", async () => {
    const tool = { ...dag.nodes[0]!, id: "tool:1", kind: "tool" as const, label: "run sql readonly", detail: { tool_name: "run_sql_readonly" }, start_seq: 3 };
    const preparation = { ...dag.nodes[0]!, id: "preparation:1", kind: "preparation" as const, label: "Preparation · run opening", detail: { phase: "run_opening" }, start_seq: 1 };
    const artifacts = [11, 21].map((seq) => ({ ...dag.nodes[0]!, id: `artifact:${seq}`, kind: "artifact" as const, label: "查询结果", start_seq: seq }));
    const custom = { ...dag.nodes[0]!, id: "custom:1", label: "Customer-specific validation", start_seq: 30 };
    const stagedDag: TraceDag = {
      ...dag,
      nodes: [preparation, ...dag.nodes, tool, ...artifacts, custom],
      sections: [
        { ...dag.sections[0]!, node_ids: [dag.nodes[0]!.id, tool.id] },
        { ...dag.sections[0]!, id: "run:run_1:section:preparation", title: preparation.label, start_seq: 1, node_ids: [preparation.id] },
        { ...dag.sections[0]!, id: "custom-section", title: "业务复核", start_seq: 30, node_ids: [custom.id] },
      ],
    };
    const selected: string[] = [];
    const fullscreen = vi.fn();
    const app = createApp(RunTraceDag, { dag: stagedDag, allowFullscreen: true, onSelectNode: (node: typeof tool) => selected.push(node.id), onFullscreen: fullscreen });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelector(".trace-dag-canvas")).toBeNull();
    expect(host.textContent).toContain("分析准备");
    expect(host.textContent).toContain("第 1 轮分析");
    expect(host.textContent).toContain("业务复核");
    const heading = host.querySelector<HTMLButtonElement>('.trace-stage-heading[title="Agent turn 1"]')!;
    expect(heading.textContent).toContain("2 项");
    expect(heading.getAttribute("aria-expanded")).toBe("false");
    heading.click();
    await nextTick();
    expect(host.querySelectorAll(".trace-stage-nodes .trace-stage-node")).toHaveLength(2);
    const toolButton = host.querySelector<HTMLButtonElement>('.trace-stage-node[title="run sql readonly"]')!;
    expect(toolButton.textContent).toContain("查询数据");
    toolButton.click();
    expect(selected).toEqual([tool.id]);
    const standalone = [...host.querySelectorAll<HTMLButtonElement>(".trace-stage-standalone")];
    expect(standalone.map((node) => node.textContent)).toEqual([expect.stringContaining("事件 #11"), expect.stringContaining("事件 #21")]);
    standalone[1]!.click();
    expect(selected).toEqual([tool.id, artifacts[1]!.id]);
    host.querySelector<HTMLButtonElement>('.trace-stage-heading[title="业务复核"]')!.click();
    await nextTick();
    expect(host.querySelector('.trace-stage-node[title="Customer-specific validation"]')?.textContent).toContain("Customer-specific validation");
    host.querySelector<HTMLButtonElement>('[title="全屏查看"]')!.click();
    expect(fullscreen).toHaveBeenCalledOnce();

    host.querySelector<HTMLInputElement>('input[value="graph"]')!.click();
    await nextTick();
    expect(host.querySelector<HTMLInputElement>('input[value="graph"]')!.checked).toBe(true);
    expect(host.querySelector(".trace-stage-list")).toBeNull();
    expect(host.querySelector(".trace-dag-canvas")).not.toBeNull();
    expect(host.querySelector(".trace-minimap")).toBeNull();
    expect(host.querySelector(".trace-canvas-hint")).toBeNull();
    expect([...host.querySelectorAll(".trace-node-label")].map((label) => label.textContent)).toContain("第 1 轮分析");
    host.querySelector<HTMLInputElement>('input[value="stages"]')!.click();
    await nextTick();
    expect(host.querySelector('.trace-stage-node[title="run sql readonly"]')).not.toBeNull();
    expect(selected).toHaveLength(2);
    app.unmount();
  });

  it("measures the fullscreen canvas and refits after its actual dimensions change", async () => {
    let resize: (() => void) | undefined;
    const disconnected = vi.fn();
    class ResizeObserverStub {
      constructor(callback: ResizeObserverCallback) { resize = () => callback([], this); }
      observe() {}
      unobserve() {}
      disconnect() { disconnected(); }
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    const bounds = vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 900, 440));
    const app = createApp(RunTraceDag, { dag: chainDag(10), mode: "overlay" });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await vi.waitFor(() => expect(host.querySelector(".trace-dag-canvas")?.getAttribute("viewBox")).toBe("0 0 900 440"));
    const initial = cameraTransform(host);
    bounds.mockReturnValue(new DOMRect(0, 0, 1280, 600));
    resize!();
    await nextTick();
    expect(host.querySelector(".trace-dag-canvas")?.getAttribute("viewBox")).toBe("0 0 1280 600");
    expect(cameraTransform(host).scale).toBeGreaterThan(initial.scale);
    app.unmount();
    expect(disconnected).toHaveBeenCalled();
  });

  it("renders persisted node actions in seq order without exposing hidden content", async () => {
    const app = createApp(RunTraceDag, { dag, selectedNodeId: dag.nodes[0]?.id });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelectorAll(".trace-stage-node")).toHaveLength(1);
    expect(host.textContent).toContain("2 条动作");
    expect(host.textContent).toContain("#2");
    expect(host.textContent).toContain("#3");
    expect(host.textContent).not.toContain("未记录可验证原因");
    expect(host.querySelector(".trace-action-reason")).toBeNull();
    expect(host.textContent).not.toContain("prompt");
    expect(host.querySelector(".trace-stage-list")).not.toBeNull();
    expect(host.querySelector(".trace-progress-list")).toBeNull();
    expect(host.querySelector(".trace-dag-canvas")).toBeNull();
    app.unmount();
  });

  it("shows a clear warning only when a failed action has no reason", async () => {
    const failedDag: TraceDag = {
      ...dag,
      nodes: [{
        ...dag.nodes[0]!,
        status: "failed",
        action_records: [{
          ...dag.nodes[0]!.action_records[0]!,
          status: "failed",
        }],
      }],
    };
    const app = createApp(RunTraceDag, { dag: failedDag, selectedNodeId: failedDag.nodes[0]?.id });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.textContent).toContain("失败原因未提供");
    expect(host.querySelector(".trace-action-reason-missing")).not.toBeNull();
    app.unmount();
  });

  it("supports keyboard node selection and keeps unresolved relations visible", async () => {
    const unresolvedDag: TraceDag = {
      ...dag,
      sections: [],
      warnings: ["TRACE_DAG_TOOL_TURN_UNRESOLVED"],
      nodes: [{ ...dag.nodes[0]!, relationship_status: "unresolved" }],
    };
    const selected: string[] = [];
    const app = createApp(RunTraceDag, {
      dag: unresolvedDag,
      mode: "overlay",
      onSelectNode: (node: typeof unresolvedDag.nodes[number]) => selected.push(node.id),
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const node = host.querySelector<SVGGElement>(".trace-node");
    expect(node?.classList.contains("unresolved")).toBe(true);
    expect(host.textContent).toContain("部分历史关系无法验证");
    node?.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    await nextTick();
    expect(selected).toEqual([unresolvedDag.nodes[0]?.id]);
    node?.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await nextTick();
    expect(selected).toEqual([unresolvedDag.nodes[0]?.id, unresolvedDag.nodes[0]?.id]);
    app.unmount();
  });

  it("does not emit twice for one pointer click", async () => {
    const selected: string[] = [];
    const app = createApp(RunTraceDag, {
      dag: { ...dag, sections: [] },
      mode: "overlay",
      onSelectNode: (node: typeof dag.nodes[number]) => selected.push(node.id),
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const node = host.querySelector<SVGGElement>(".trace-node");
    node?.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    node?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();

    expect(selected).toEqual([dag.nodes[0]?.id]);
    app.unmount();
  });

  it("keeps folded feedback relationships visible without inflating ranks or overlapping nodes", async () => {
    const foldedDag = chainDag(5);
    foldedDag.sections = [{
      ...dag.sections[0]!,
      id: "noncontiguous-section",
      node_ids: [foldedDag.nodes[1]!.id, foldedDag.nodes[3]!.id],
    }];
    const app = createApp(RunTraceDag, { dag: foldedDag, mode: "overlay" });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const positions = nodePositions(host);
    expect(positions).toHaveLength(4);
    expect(host.querySelectorAll(".trace-edge")).toHaveLength(4);
    expect(new Set(positions.map((node) => node.y)).size).toBe(3);
    const ranks = [...new Set(positions.map((node) => node.y))].sort((left, right) => left - right);
    expect(ranks[1]! - ranks[0]!).toBe(ranks[2]! - ranks[1]!);
    const sameRank = positions.filter((node) => node.y === ranks[1]).sort((left, right) => left.x - right.x);
    const pillWidth = Number(host.querySelector(".trace-node-pill")?.getAttribute("width"));
    expect(sameRank).toHaveLength(2);
    expect(sameRank[1]!.x - sameRank[0]!.x).toBeGreaterThan(pillWidth);

    host.querySelector<HTMLButtonElement>(".trace-section-chip")?.click();
    await nextTick();
    expect(host.querySelectorAll(".trace-node")).toHaveLength(5);
    expect(host.querySelectorAll(".trace-edge")).toHaveLength(4);
    expect(new Set(nodePositions(host).map((node) => node.y)).size).toBe(5);
    app.unmount();
  });

  describe.each(["embedded", "overlay"] as const)("%s fit", (mode) => {
    it.each([false, true])("fits every node and keeps zoom controls consistent when the trace is folded=%s", async (folded) => {
      const largeDag = chainDag(60);
      if (folded) {
        largeDag.sections = [{
          ...dag.sections[0]!,
          id: "wide-feedback-section",
          node_ids: [largeDag.nodes[0]!.id, largeDag.nodes[59]!.id],
        }];
      }
      const app = createApp(RunTraceDag, { dag: largeDag, mode });
      const host = document.createElement("div");
      document.body.append(host);
      app.mount(host);
      await nextTick();
      if (mode === "embedded") {
        host.querySelector<HTMLInputElement>('input[value="graph"]')!.click();
        await nextTick();
        expect(host.querySelector(".trace-minimap")).toBeNull();
      }

      const zoomOut = host.querySelector<HTMLButtonElement>('[title="缩小"]')!;
      const zoomIn = host.querySelector<HTMLButtonElement>('[title="放大"]')!;
      const fit = host.querySelector<HTMLButtonElement>('[title="适合画布"]')!;
      const fitted = cameraTransform(host);
      expect(fitted.scale).toBeLessThan(0.42);
      expect(zoomOut.disabled).toBe(true);
      zoomIn.click();
      await nextTick();
      const enlarged = cameraTransform(host);
      expect(enlarged.scale).toBeGreaterThan(fitted.scale);
      expect(zoomOut.disabled).toBe(false);
      zoomOut.click();
      await nextTick();
      expect(cameraTransform(host).scale).toBeLessThan(enlarged.scale);
      zoomOut.click();
      await nextTick();
      expect(cameraTransform(host).scale).toBeCloseTo(fitted.scale);
      expect(zoomOut.disabled).toBe(true);

      zoomIn.click();
      await nextTick();
      fit.click();
      await nextTick();
      const reset = cameraTransform(host);
      expect(reset).toEqual(fitted);
      const viewport = host.querySelector(".trace-dag-canvas")!.getAttribute("viewBox")!.split(" ").map(Number);
      const positions = nodePositions(host);
      expect(positions).toHaveLength(folded ? 59 : 60);
      expect(host.querySelectorAll(".trace-edge")).toHaveLength(59);
      for (const { x, y } of positions) {
        expect(reset.x + (x - 66) * reset.scale).toBeGreaterThanOrEqual(0);
        expect(reset.x + (x + 100) * reset.scale).toBeLessThanOrEqual(viewport[2]!);
        expect(reset.y + (y - 18) * reset.scale).toBeGreaterThanOrEqual(0);
        expect(reset.y + (y + 34) * reset.scale).toBeLessThanOrEqual(viewport[3]!);
      }
      app.unmount();
    });
  });
});
