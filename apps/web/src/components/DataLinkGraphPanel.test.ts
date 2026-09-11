import { createApp, h, nextTick, ref } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DataLinkCatalog, DataLinkCatalogItem, DataLinkSubgraph } from "@/api/types";

const echarts = vi.hoisted(() => ({
  use: vi.fn(),
  init: vi.fn(),
  graphChart: Symbol("GraphChart"),
  tooltip: Symbol("TooltipComponent"),
  canvas: Symbol("CanvasRenderer"),
  labelLayout: Symbol("LabelLayout"),
}));

const customerEntity: DataLinkCatalogItem = {
  node: {
    id: "entity:customer",
    type: "entity",
    name: "客户",
    description: "下单的客户",
    aliases: [],
    table: null,
    semantic_type: null,
    profile: null,
  },
  provenance: "semantic_mapping",
  mapping_count: 2,
  relation_count: 2,
  mapped_columns: [{ table: "customers", name: "email" }],
};

const catalog: DataLinkCatalog = {
  datasource_id: "ds_1",
  graph_version: "graph_1",
  total: 1,
  page: 1,
  page_size: 100,
  items: [customerEntity],
};

const subgraph: DataLinkSubgraph = {
  datasource_id: "ds_1",
  graph_version: "graph_1",
  root_node_id: "entity:customer",
  total_node_count: 6,
  total_edge_count: 5,
  nodes: [
    {
      id: "table:customers",
      type: "table",
      name: "customers",
      description: null,
      aliases: [],
      table: "customers",
      semantic_type: null,
      profile: null,
    },
    {
      id: "column:customers.email",
      type: "column",
      name: "email",
      description: null,
      aliases: [],
      table: "customers",
      semantic_type: null,
      profile: null,
    },
    {
      id: "concept:email",
      type: "concept",
      name: "邮箱",
      description: "客户联系邮箱",
      aliases: [],
      table: null,
      semantic_type: null,
      profile: null,
    },
    {
      id: "entity:customer",
      type: "entity",
      name: "客户",
      description: "下单的客户",
      aliases: [],
      table: null,
      semantic_type: null,
      profile: null,
    },
  ],
  edges: [
    {
      source: "table:customers",
      target: "column:customers.email",
      type: "contains",
      confidence: 1,
      evidence: null,
    },
    {
      source: "column:customers.email",
      target: "column:orders.email",
      type: "joinable",
      confidence: 1,
      evidence: { kind: "value_overlap", summary: "sampled value overlap is 1.0" },
    },
    {
      source: "column:customers.email",
      target: "concept:email",
      type: "represents",
      confidence: 0.8,
      evidence: null,
    },
    {
      source: "entity:customer",
      target: "concept:email",
      type: "has_concept",
      confidence: 0.8,
      evidence: null,
    },
  ],
  is_truncated: false,
  warnings: [],
};

const panelProps = {
  catalog,
  subgraph,
  graphVersion: "graph_1",
  entryType: "entity" as const,
  entryQuery: "",
  edgeTypes: [] as [],
  currentRootNodeId: "entity:customer",
  initialRootNodeId: "entity:customer",
};

vi.mock("echarts/core", () => ({ init: echarts.init, use: echarts.use }));
vi.mock("echarts/charts", () => ({ GraphChart: echarts.graphChart }));
vi.mock("echarts/components", () => ({ TooltipComponent: echarts.tooltip }));
vi.mock("echarts/features", () => ({ LabelLayout: echarts.labelLayout }));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: echarts.canvas }));

describe("DataLinkGraphPanel", () => {
  beforeEach(() => {
    vi.resetModules();
    echarts.use.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("注册图谱 option 用到的 ECharts 模块", async () => {
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, {
      catalog: null,
      subgraph: null,
      graphVersion: null,
      entryType: "entity",
      entryQuery: "",
      edgeTypes: [],
      currentRootNodeId: null,
      initialRootNodeId: null,
    });
    const host = document.createElement("div");
    app.mount(host);
    await nextTick();

    expect(echarts.use).toHaveBeenCalledWith([
      echarts.graphChart,
      echarts.tooltip,
      echarts.canvas,
      echarts.labelLayout,
    ]);
    app.unmount();
  });

  it("observes a canvas loaded asynchronously and rebinds after it is replaced", async () => {
    const chart = { setOption: vi.fn(), on: vi.fn(), dispose: vi.fn(), resize: vi.fn() };
    const observe = vi.fn();
    const disconnect = vi.fn();
    let notifyResize = () => {};
    class ResizeObserverStub {
      constructor(callback: () => void) { notifyResize = callback; }
      observe = observe;
      disconnect = disconnect;
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);
    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const currentSubgraph = ref<typeof subgraph | null>(null);
    const app = createApp(() => h(DataLinkGraphPanel, {
      catalog: null,
      subgraph: currentSubgraph.value,
      graphVersion: "graph_1",
      entryType: "entity",
      entryQuery: "",
      edgeTypes: [],
      currentRootNodeId: "entity:customer",
      initialRootNodeId: "entity:customer",
    }));
    const host = document.createElement("div");
    app.mount(host);
    await nextTick();
    expect(observe).not.toHaveBeenCalled();

    currentSubgraph.value = subgraph;
    await nextTick();
    await nextTick();
    const firstCanvas = host.querySelector(".graph-canvas");
    expect(firstCanvas).not.toBeNull();
    expect(observe).toHaveBeenLastCalledWith(firstCanvas);
    notifyResize();
    expect(chart.resize).toHaveBeenCalledOnce();

    currentSubgraph.value = null;
    await nextTick();
    await nextTick();
    expect(host.querySelector(".graph-canvas")).toBeNull();
    expect(disconnect).toHaveBeenCalled();

    currentSubgraph.value = subgraph;
    await nextTick();
    await nextTick();
    const nextCanvas = host.querySelector(".graph-canvas");
    expect(nextCanvas).not.toBe(firstCanvas);
    expect(observe).toHaveBeenLastCalledWith(nextCanvas);
    notifyResize();
    expect(chart.resize).toHaveBeenCalledTimes(2);
    app.unmount();
  });

  it("浏览入口列出实体，点击属性后详情显示映射字段", async () => {
    const chart = {
      setOption: vi.fn(),
      on: vi.fn(),
      dispose: vi.fn(),
      resize: vi.fn(),
    };
    const focusNode = vi.fn();
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);

    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, {
      ...panelProps,
      onFocusNode: focusNode,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    expect(host.textContent).toContain("1 个业务实体");
    expect(host.textContent).toContain("客户");
    expect(host.textContent).toContain("2 个映射字段");
    expect(host.querySelector(".entry-type-switch")?.textContent).toContain("实体");
    expect(host.querySelector(".entry-type-switch")?.textContent).toContain("属性");
    expect(host.querySelector(".entry-type-switch")?.textContent).not.toContain("表");
    const inspector = host.querySelector(".graph-inspector");
    expect(inspector?.textContent).toContain("已选中");
    expect(inspector?.textContent).toContain("下单的客户");
    expect(inspector?.textContent).toContain("关联属性");
    expect(inspector?.textContent).toContain("邮箱");
    expect(inspector?.textContent).toContain("customers.email");
    expect(inspector?.textContent).toContain("语义节点");
    expect(inspector?.textContent).not.toContain("全图节点");
    expect(inspector?.textContent).not.toContain("未选择");
    expect(inspector?.querySelector(".inspector-kind.is-entity")).not.toBeNull();
    const conceptChip = [...inspector?.querySelectorAll("button.inspector-chip.is-concept") ?? []].find(
      (button) => button.textContent === "邮箱",
    );
    expect(conceptChip).toBeDefined();
    conceptChip?.click();
    await nextTick();
    expect(inspector?.querySelector(".inspector-kind.is-concept")).not.toBeNull();
    expect(inspector?.textContent).toContain("客户联系邮箱");
    expect(inspector?.textContent).toContain("所属实体");

    const clickHandler = chart.on.mock.calls.find(([eventName]) => eventName === "click")?.[1];
    expect(clickHandler).toEqual(expect.any(Function));
    const initCount = echarts.init.mock.calls.length;
    clickHandler({ dataType: "node", data: { id: "concept:email" } });
    await nextTick();

    expect(echarts.init.mock.calls.length).toBe(initCount);
    const restyle = chart.setOption.mock.calls.at(-1)?.[0]?.series?.[0]?.data;
    expect(restyle).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "entity:customer", symbol: "roundRect", symbolSize: [82, 40] }),
      expect.objectContaining({ id: "concept:email", symbol: "circle", symbolSize: 22 }),
    ]));

    expect(host.textContent).toContain("业务属性");
    expect(host.textContent).toContain("客户联系邮箱");
    expect(host.textContent).toContain("所属实体");
    expect(host.textContent).toContain("customers.email");
    expect(host.textContent).not.toContain("语义类型");
    expect(host.textContent).not.toContain("未识别");

    const focusButton = [...host.querySelectorAll("button")].find(
      (button) => button.textContent?.includes("聚焦此节点"),
    );
    expect(focusButton).toBeDefined();
    focusButton?.click();
    expect(focusNode).toHaveBeenCalledWith("concept:email");

    app.unmount();
    host.remove();
  });

  it("属性入口按所属实体分组，不把不同对象的含义平铺在一起", async () => {
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    const selectEntry = vi.fn();
    const eventEntity: DataLinkCatalogItem = {
      ...customerEntity,
      node: { ...customerEntity.node, id: "entity:event", name: "事件" },
      mapped_columns: [{ table: "events", name: "event_id" }],
    };
    const productEntity: DataLinkCatalogItem = {
      ...customerEntity,
      node: { ...customerEntity.node, id: "entity:product", name: "产品" },
      mapped_columns: [{ table: "products", name: "product_name" }],
    };
    const eventId: DataLinkCatalogItem = {
      ...customerEntity,
      node: { ...customerEntity.node, id: "concept:event-id", type: "concept", name: "事件唯一标识" },
      mapped_columns: [{ table: "events", name: "event_id" }],
    };
    const productName: DataLinkCatalogItem = {
      ...customerEntity,
      node: { ...customerEntity.node, id: "concept:product-name", type: "concept", name: "产品名称" },
      mapped_columns: [{ table: "products", name: "product_name" }],
    };
    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, {
      ...panelProps,
      catalog: {
        ...catalog,
        total: 2,
        items: [eventId, productName],
      },
      entityIndex: [eventEntity, productEntity],
      entryType: "concept",
      subgraph: null,
      currentRootNodeId: null,
      initialRootNodeId: null,
      onSelectEntry: selectEntry,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    const groups = [...host.querySelectorAll(".entry-group")];
    expect(groups.map((group) => group.querySelector(".entry-group-label strong")?.textContent)).toEqual(["产品", "事件"]);
    expect(groups[0].textContent).toContain("产品名称");
    expect(groups[0].textContent).not.toContain("事件唯一标识");
    expect(groups[1].textContent).toContain("事件唯一标识");
    expect(groups[1].textContent).not.toContain("产品名称");

    groups[1].querySelector<HTMLButtonElement>(".entry-group-label")?.click();
    expect(selectEntry).toHaveBeenCalledWith(eventEntity);

    app.unmount();
    host.remove();
  });

  it("画布只画实体和属性，不把表、字段或 Join 边画进去", async () => {
    const chart = {
      setOption: vi.fn(),
      on: vi.fn(),
      dispose: vi.fn(),
      resize: vi.fn(),
    };
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);

    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, panelProps);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    const series = chart.setOption.mock.calls[0]?.[0]?.series?.[0];
    expect(series).toEqual(expect.objectContaining({
      layout: "none",
      roam: true,
      roamTrigger: "global",
      draggable: false,
      nodeScaleRatio: 0,
      preserveAspect: true,
      data: expect.arrayContaining([
        expect.objectContaining({
          id: "entity:customer",
          name: "客户",
          symbol: "roundRect",
          symbolSize: [82, 40],
          label: expect.objectContaining({ position: "inside" }),
        }),
        expect.objectContaining({
          id: "concept:email",
          name: "邮箱",
          symbol: "circle",
          symbolSize: 16,
          label: expect.objectContaining({ position: "top" }),
        }),
      ]),
      links: [
        expect.objectContaining({
          source: "entity:customer",
          target: "concept:email",
          lineStyle: expect.objectContaining({ type: "solid", curveness: 0.08 }),
        }),
      ],
      labelLayout: { hideOverlap: true, moveOverlap: "shiftY" },
    }));
    expect(series.data).toHaveLength(2);
    expect(series.data.some((item: { id: string }) => item.id.startsWith("table:") || item.id.startsWith("column:"))).toBe(false);
    expect(host.textContent).toContain("2 个语义节点，1 条语义关系");
    expect(host.textContent).not.toContain("数据库外键");
    expect(host.textContent).not.toContain("候选关联");
    expect(host.textContent).toContain("实体归属");
    expect(host.textContent).toContain("语义同义");

    app.unmount();
    host.remove();
  });

  it("让节点拖拽不抢占画布平移", async () => {
    const chart = {
      setOption: vi.fn(),
      on: vi.fn(),
      dispose: vi.fn(),
      resize: vi.fn(),
    };
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);

    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, panelProps);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    const series = chart.setOption.mock.calls[0]?.[0]?.series?.[0];
    expect(series).toEqual(expect.objectContaining({
      roam: true,
      roamTrigger: "global",
      draggable: false,
      nodeScaleRatio: 0,
      preserveAspect: true,
    }));
    expect(series.cursor).toBeUndefined();

    app.unmount();
    host.remove();
  });

  it("切换主题后重绘图谱", async () => {
    const chart = {
      setOption: vi.fn(),
      on: vi.fn(),
      dispose: vi.fn(),
      resize: vi.fn(),
    };
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);

    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const { applyTheme } = await import("@/lib/theme");
    const app = createApp(DataLinkGraphPanel, panelProps);
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    const callsBefore = chart.setOption.mock.calls.length;
    applyTheme("light");
    await nextTick();
    await nextTick();
    expect(chart.setOption.mock.calls.length).toBeGreaterThan(callsBefore);

    applyTheme("dark");
    app.unmount();
    host.remove();
  });

  it("空筛选显示全部语义关系，勾选一类后只请求该类", async () => {
    const chart = { setOption: vi.fn(), on: vi.fn(), dispose: vi.fn(), resize: vi.fn() };
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);
    const updateEdgeTypes = vi.fn();
    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, {
      ...panelProps,
      onUpdateEdgeTypes: updateEdgeTypes,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    expect(host.textContent).toContain("当前显示全部语义关系");
    expect(host.querySelector<HTMLInputElement>(".relation-all input")?.checked).toBe(true);
    const ownership = [...host.querySelectorAll(".relation-options label")].find((label) => label.textContent?.includes("实体归属"));
    const ownershipInput = ownership?.querySelector("input") as HTMLInputElement;
    ownershipInput.checked = true;
    ownershipInput.dispatchEvent(new Event("change", { bubbles: true }));
    expect(updateEdgeTypes).toHaveBeenCalledWith(["has_concept"]);

    app.unmount();
    host.remove();
  });

  it("点全部会清空类型筛选", async () => {
    const chart = { setOption: vi.fn(), on: vi.fn(), dispose: vi.fn(), resize: vi.fn() };
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);
    const updateEdgeTypes = vi.fn();
    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, {
      ...panelProps,
      edgeTypes: ["has_concept"],
      onUpdateEdgeTypes: updateEdgeTypes,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    expect(host.textContent).toContain("仅显示已勾选类型");
    const allInput = host.querySelector<HTMLInputElement>(".relation-all input")!;
    expect(allInput.checked).toBe(false);
    allInput.checked = true;
    allInput.dispatchEvent(new Event("change", { bubbles: true }));
    expect(updateEdgeTypes).toHaveBeenCalledWith([]);

    app.unmount();
    host.remove();
  });

  it("内嵌模式可打开全屏，overlay 不再显示全屏按钮", async () => {
    const chart = { setOption: vi.fn(), on: vi.fn(), dispose: vi.fn(), resize: vi.fn() };
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(320);
    echarts.init.mockReturnValue(chart);
    const fullscreen = vi.fn();
    const { default: DataLinkGraphPanel } = await import("./DataLinkGraphPanel.vue");
    const app = createApp(DataLinkGraphPanel, {
      ...panelProps,
      allowFullscreen: true,
      onFullscreen: fullscreen,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();
    await nextTick();

    host.querySelector<HTMLButtonElement>('[title="全屏查看"]')!.click();
    expect(fullscreen).toHaveBeenCalledOnce();
    app.unmount();
    host.remove();

    const overlayApp = createApp(DataLinkGraphPanel, {
      ...panelProps,
      mode: "overlay",
      allowFullscreen: true,
    });
    const overlayHost = document.createElement("div");
    document.body.append(overlayHost);
    overlayApp.mount(overlayHost);
    await nextTick();
    await nextTick();

    expect(overlayHost.querySelector('[title="全屏查看"]')).toBeNull();
    expect(overlayHost.querySelector(".graph-panel")?.classList.contains("is-overlay")).toBe(true);
    const series = chart.setOption.mock.calls.at(-1)?.[0]?.series?.[0];
    expect(series.layout).toBe("none");
    expect(series.force).toBeUndefined();

    overlayApp.unmount();
    overlayHost.remove();
  });
});
