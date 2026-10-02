<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { init, use, type ComposeOption, type ECharts } from "echarts/core";
import { GraphChart, type GraphSeriesOption } from "echarts/charts";
import { TooltipComponent } from "echarts/components";
import { LabelLayout } from "echarts/features";
import { CanvasRenderer } from "echarts/renderers";
import { Focus, Maximize2, RotateCcw, Search } from "@lucide/vue";

import type {
  DataLinkCatalog,
  DataLinkCatalogItem,
  DataLinkEdge,
  DataLinkEdgeType,
  DataLinkBrowserNode,
  DataLinkNodeType,
  DataLinkSubgraph,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  datalinkColumnRef,
  datalinkMappedColumnList,
  datalinkNodeLabels,
  datalinkRelationLabels,
  datalinkRelationProvenanceLabels,
  groupConceptsByOwningEntity,
} from "@/lib/datalinkDisplay";
import {
  conceptLabelOrigin,
  LAYOUT_CENTER,
  layoutSemanticMap,
  mappedFieldLabels,
  outwardLabelPosition,
  owningEntities,
  projectSemanticMap,
  relatedConcepts,
  SEMANTIC_CANVAS_EDGE_TYPES,
} from "@/lib/datalinkSemanticMap";
import { useTheme } from "@/lib/theme";

type SemanticBrowseType = Extract<DataLinkNodeType, "concept" | "entity">;

const props = withDefaults(defineProps<{
  catalog: DataLinkCatalog | null;
  entityIndex?: DataLinkCatalogItem[];
  subgraph: DataLinkSubgraph | null;
  graphVersion: string | null;
  entryType: SemanticBrowseType;
  entryQuery: string;
  edgeTypes: DataLinkEdgeType[];
  currentRootNodeId: string | null;
  initialRootNodeId: string | null;
  loadingCatalog?: boolean;
  loadingSubgraph?: boolean;
  errorMessage?: string | null;
  mode?: "embedded" | "overlay";
  allowFullscreen?: boolean;
}>(), {
  entityIndex: () => [],
  loadingCatalog: false,
  loadingSubgraph: false,
  errorMessage: null,
  mode: "embedded",
  allowFullscreen: false,
});

const emit = defineEmits<{
  selectEntry: [item: DataLinkCatalogItem];
  updateEntryType: [type: SemanticBrowseType];
  updateEntryQuery: [query: string];
  updateEdgeTypes: [types: DataLinkEdgeType[]];
  focusNode: [nodeId: string];
  resetRoot: [];
  fullscreen: [];
}>();

use([GraphChart, TooltipComponent, CanvasRenderer, LabelLayout]);
const { theme } = useTheme();

const graphElement = ref<HTMLDivElement | null>(null);
const selectedNode = ref<DataLinkBrowserNode | null>(null);
const selectedEdge = ref<DataLinkEdge | null>(null);
let chart: ECharts | null = null;
let observer: ResizeObserver | null = null;

const GRAPH_FONT = 'Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif';
const canvasRelationTypes = SEMANTIC_CANVAS_EDGE_TYPES.map((type) => [type, datalinkRelationLabels[type]] as const);
const relationLegend = [
  { key: "has_concept", label: datalinkRelationLabels.has_concept, className: "has-concept" },
  { key: "semantic_synonym", label: datalinkRelationLabels.semantic_synonym, className: "synonym" },
] as const;
const semanticMap = computed(() => (props.subgraph ? projectSemanticMap(props.subgraph) : null));
const currentRootName = computed(() => {
  const rootNodeId = props.currentRootNodeId;
  if (!rootNodeId) return null;
  return props.subgraph?.nodes.find((node) => node.id === rootNodeId)?.name
    ?? props.catalog?.items.find((item) => item.node.id === rootNodeId)?.node.name
    ?? null;
});
const activeEntryTypeLabel = computed(() => datalinkNodeLabels[props.entryType]);
const selectedMappedFields = computed(() => {
  if (selectedNode.value === null || props.subgraph === null) return [];
  const catalogFields = props.catalog?.items.find((item) => item.node.id === selectedNode.value?.id);
  if (catalogFields?.mapped_columns?.length) return catalogFields.mapped_columns.map(datalinkColumnRef);
  return mappedFieldLabels(props.subgraph, selectedNode.value);
});
const selectedMappedFieldParts = computed(() => selectedMappedFields.value.map((field) => {
  const index = field.lastIndexOf(".");
  return index > 0
    ? { ref: field, table: field.slice(0, index), column: field.slice(index + 1) }
    : { ref: field, table: null, column: field };
}));
const selectedConcepts = computed(() => (
  selectedNode.value?.type === "entity" && props.subgraph
    ? relatedConcepts(props.subgraph, selectedNode.value.id)
    : []
));
const selectedEntities = computed(() => (
  selectedNode.value?.type === "concept" && props.subgraph
    ? owningEntities(props.subgraph, selectedNode.value.id)
    : []
));
const conceptGroups = computed(() => (
  props.entryType === "concept" && props.catalog && props.entityIndex.length > 0
    ? groupConceptsByOwningEntity(props.catalog.items, props.entityIndex)
    : []
));
const showConceptGroups = computed(() => conceptGroups.value.length > 0);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function edgeKey(edge: Pick<DataLinkEdge, "source" | "target" | "type">): string {
  return `${edge.source}\u0000${edge.target}\u0000${edge.type}`;
}

function cssColor(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value.length > 0 ? value : fallback;
}

function withAlpha(color: string, alpha: number): string {
  const hex = color.match(/^#([\da-f]{3}|[\da-f]{6})$/i);
  if (hex) {
    let value = hex[1];
    if (value.length === 3) value = [...value].map((digit) => `${digit}${digit}`).join("");
    const red = Number.parseInt(value.slice(0, 2), 16);
    const green = Number.parseInt(value.slice(2, 4), 16);
    const blue = Number.parseInt(value.slice(4, 6), 16);
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
  }
  const rgb = color.match(/^rgba?\(([^)]+)\)$/i);
  if (rgb) {
    const [red, green, blue] = rgb[1].split(",").map((part) => part.trim());
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
  }
  return color;
}

function graphChrome(): {
  label: string;
  muted: string;
  border: string;
  inset: string;
  datalink: string;
  concept: string;
  primary: string;
} {
  return {
    label: cssColor("--workspace-text", "#c2c9d2"),
    muted: cssColor("--workspace-text-muted", "#9ba4b2"),
    border: cssColor("--workspace-border-strong", "#3a424e"),
    inset: cssColor("--workspace-surface-inset", "#0b0d10"),
    datalink: cssColor("--workspace-tool-datalink", "#a99aff"),
    concept: cssColor("--workspace-tool-sql", "#6bb8d1"),
    primary: cssColor("--primary", "#d94b32"),
  };
}

function edgeVisual(edgeType: DataLinkEdgeType): { color: string; width: number; type: "solid" | "dashed"; opacity: number; curveness: number } {
  const chrome = graphChrome();
  if (edgeType === "has_concept") {
    return { color: withAlpha(chrome.datalink, 0.42), width: 1.35, type: "solid", opacity: 1, curveness: 0.08 };
  }
  return { color: withAlpha(chrome.muted, 0.72), width: 1.2, type: "dashed", opacity: 1, curveness: 0.18 };
}

function entitySymbolSize(name: string, featured: boolean): [number, number] {
  const width = Math.min(128, Math.max(72, 32 + [...name].length * 13));
  return featured ? [width + 10, 40] : [width, 34];
}

function nodeMarks(nodeId: string, subgraph: DataLinkSubgraph): { featured: boolean; selected: boolean } {
  const selected = selectedNode.value?.id === nodeId;
  return { selected, featured: selected || nodeId === subgraph.root_node_id };
}

function seriesData(subgraph: DataLinkSubgraph): NonNullable<GraphSeriesOption["data"]> {
  const chrome = graphChrome();
  const projected = projectSemanticMap(subgraph);
  const positions = layoutSemanticMap(projected.nodes, projected.edges);
  return projected.nodes.map((node) => {
    const position = positions.get(node.id) ?? LAYOUT_CENTER;
    const { featured, selected } = nodeMarks(node.id, subgraph);
    if (node.type === "entity") {
      const size = entitySymbolSize(node.name, featured);
      return {
        id: node.id,
        name: node.name,
        category: node.type,
        x: position.x,
        y: position.y,
        symbol: "roundRect",
        symbolSize: size,
        itemStyle: {
          color: withAlpha(chrome.datalink, selected ? 0.34 : 0.2),
          borderColor: selected ? chrome.primary : chrome.datalink,
          borderWidth: selected ? 2 : 1.25,
          shadowBlur: selected ? 14 : 6,
          shadowColor: selected ? withAlpha(chrome.primary, 0.42) : withAlpha(chrome.datalink, 0.22),
        },
        label: {
          show: true,
          position: "inside",
          color: chrome.label,
          fontSize: featured ? 12 : 11,
          fontWeight: 600,
          fontFamily: GRAPH_FONT,
          overflow: "truncate",
          width: Math.max(48, size[0] - 16),
          ellipsis: "…",
        },
      };
    }
    const origin = conceptLabelOrigin(node.id, projected.edges, positions);
    return {
      id: node.id,
      name: node.name,
      category: node.type,
      x: position.x,
      y: position.y,
      symbol: "circle",
      symbolSize: selected ? 22 : 16,
      itemStyle: {
        color: withAlpha(chrome.concept, selected ? 0.95 : 0.82),
        borderColor: selected ? chrome.primary : withAlpha(chrome.concept, 0.95),
        borderWidth: selected ? 2 : 1,
        shadowBlur: selected ? 12 : 4,
        shadowColor: selected ? withAlpha(chrome.primary, 0.4) : withAlpha(chrome.concept, 0.28),
      },
      label: {
        show: true,
        position: outwardLabelPosition(position, origin),
        distance: 8,
        color: chrome.label,
        fontSize: 11,
        fontFamily: GRAPH_FONT,
        overflow: "truncate",
        width: 92,
        ellipsis: "…",
      },
    };
  });
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function tooltipText(params: unknown): string {
  if (!isRecord(params)) return "";
  const data = params.data;
  if (params.dataType === "node" && isRecord(data) && typeof data.name === "string") {
    const category = typeof data.category === "string" && data.category in datalinkNodeLabels
      ? datalinkNodeLabels[data.category as DataLinkNodeType]
      : "";
    return `${escapeHtml(category)} · ${escapeHtml(data.name)}`;
  }
  if (params.dataType === "edge" && isRecord(data) && typeof data.id === "string") {
    const [sourceId, targetId, type] = data.id.split("\u0000");
    const typeLabel = type && type in datalinkRelationLabels
      ? datalinkRelationLabels[type as DataLinkEdgeType]
      : "关系";
    return `${escapeHtml(typeLabel)} · ${escapeHtml(edgeEndpointFromId(sourceId ?? ""))} → ${escapeHtml(edgeEndpointFromId(targetId ?? ""))}`;
  }
  return "";
}

function edgeEndpointFromId(id: string): string {
  const node = props.subgraph?.nodes.find((candidate) => candidate.id === id);
  return node ? node.name : "";
}

function optionFor(subgraph: DataLinkSubgraph): ComposeOption<GraphSeriesOption> {
  const chrome = graphChrome();
  const projected = projectSemanticMap(subgraph);
  return {
    animationDuration: 180,
    backgroundColor: "transparent",
    tooltip: {
      show: true,
      trigger: "item",
      confine: true,
      backgroundColor: cssColor("--workspace-surface", "#15181d"),
      borderColor: cssColor("--workspace-border-strong", "#3a424e"),
      borderWidth: 1,
      textStyle: { color: chrome.label, fontSize: 11, fontFamily: GRAPH_FONT },
      formatter: tooltipText,
    },
    series: [{
      type: "graph",
      layout: "none",
      roam: true,
      // View.containPoint tests the node bbox; global roam lets empty padding pan/zoom.
      roamTrigger: "global",
      draggable: false,
      nodeScaleRatio: 0,
      preserveAspect: true,
      scaleLimit: { min: 0.35, max: 2.4 },
      left: 56,
      right: 56,
      top: 52,
      bottom: 52,
      zoom: props.mode === "overlay" ? 0.88 : 0.72,
      autoCurveness: 0.18,
      data: seriesData(subgraph),
      links: projected.edges.map((edge) => ({
        id: edgeKey(edge),
        source: edge.source,
        target: edge.target,
        lineStyle: edgeVisual(edge.type),
      })),
      categories: [
        { name: "entity" },
        { name: "concept" },
      ],
      label: {
        show: true,
        color: chrome.label,
        fontSize: 11,
        fontFamily: GRAPH_FONT,
      },
      labelLayout: { hideOverlap: true, moveOverlap: "shiftY" },
      lineStyle: { opacity: 1 },
      emphasis: { focus: "adjacency", scale: false },
    }],
  };
}

function edgeEndpoint(edge: DataLinkEdge, endpoint: "source" | "target"): string {
  const id = edge[endpoint];
  const node = props.subgraph?.nodes.find((candidate) => candidate.id === id);
  return node ? node.name : "当前语义地图中未显示";
}

function evidenceDescription(edge: DataLinkEdge): string {
  if (edge.type === "has_concept") return "实体拥有该业务属性，不是 Join 建议";
  if (edge.type === "semantic_synonym") return "属性之间语义相近，不是 Join 建议";
  return "用于说明字段归属或语义关系，不作为 Join 建议";
}

function entrySubtitle(item: DataLinkCatalogItem): string {
  if (item.mapping_count > 0) return `${item.mapping_count} 个映射字段`;
  const mapped = datalinkMappedColumnList(item);
  if (mapped) return mapped;
  if (item.node.aliases.length) return item.node.aliases.join("、");
  if (item.node.description) return item.node.description;
  return datalinkNodeLabels[item.node.type];
}

function semanticNodeById(id: string | null): DataLinkBrowserNode | null {
  if (!id || props.subgraph === null) return null;
  const node = props.subgraph.nodes.find((candidate) => candidate.id === id);
  if (node === undefined || (node.type !== "entity" && node.type !== "concept")) return null;
  return node;
}

function revealRootSelection(): void {
  selectedEdge.value = null;
  selectedNode.value = semanticNodeById(props.currentRootNodeId ?? props.subgraph?.root_node_id ?? null);
}

function selectInspectorNode(nodeId: string): void {
  const node = semanticNodeById(nodeId);
  if (node === null) return;
  selectedNode.value = node;
  selectedEdge.value = null;
}

function confidenceLabel(value: number | null): string {
  return value === null ? "无统计分数" : `${(value * 100).toFixed(0)}%`;
}

function confidenceWidth(value: number | null): string | null {
  if (value === null) return null;
  return `${Math.max(0, Math.min(100, value * 100)).toFixed(0)}%`;
}

function selectChartItem(params: unknown): void {
  if (props.subgraph === null || !isRecord(params)) return;
  const dataType = params.dataType;
  const data = params.data;
  if (!isRecord(data)) return;
  if (dataType === "node" && typeof data.id === "string") {
    selectedNode.value = props.subgraph.nodes.find((node) => node.id === data.id) ?? null;
    selectedEdge.value = null;
    return;
  }
  if (dataType === "edge" && typeof data.id === "string") {
    selectedEdge.value = props.subgraph.edges.find((edge) => edgeKey(edge) === data.id) ?? null;
    selectedNode.value = null;
  }
}

function showingAllRelations(): boolean {
  return props.edgeTypes.length === 0;
}

function toggleEdgeType(edgeType: DataLinkEdgeType, checked: boolean): void {
  const next = checked
    ? [...new Set([...props.edgeTypes, edgeType])]
    : props.edgeTypes.filter((candidate) => candidate !== edgeType);
  emit("updateEdgeTypes", next);
}

function toggleAllEdgeTypes(checked: boolean): void {
  if (checked || props.edgeTypes.length > 0) emit("updateEdgeTypes", []);
}

function checkedFromEvent(event: Event): boolean {
  return event.target instanceof HTMLInputElement && event.target.checked;
}

function disposeChart(): void {
  chart?.dispose();
  chart = null;
}

function renderGraph(): void {
  if (graphElement.value === null || semanticMap.value === null || semanticMap.value.nodes.length === 0) {
    disposeChart();
    return;
  }
  if (graphElement.value.clientWidth === 0 || graphElement.value.clientHeight === 0) return;
  if (props.subgraph === null) return;
  disposeChart();
  chart = init(graphElement.value, undefined, { renderer: "canvas" });
  chart.setOption(optionFor(props.subgraph), { notMerge: true });
  chart.on("click", selectChartItem);
}

function restyleSelection(): void {
  if (chart === null || props.subgraph === null) return;
  chart.setOption({ series: [{ data: seriesData(props.subgraph) }] });
}

function resizeGraph(): void {
  if (chart === null) {
    renderGraph();
    return;
  }
  chart.resize();
}

watch(
  () => props.subgraph,
  () => {
    revealRootSelection();
    void nextTick(renderGraph);
  },
  { immediate: true },
);

watch(selectedNode, () => {
  restyleSelection();
});

watch(theme, () => {
  void nextTick(renderGraph);
});

watch(() => props.mode, () => {
  void nextTick(renderGraph);
});

watch(graphElement, (element) => {
  observer?.disconnect();
  if (element !== null) observer?.observe(element);
}, { flush: "post" });

onMounted(() => {
  observer = new ResizeObserver(resizeGraph);
  if (graphElement.value !== null) observer.observe(graphElement.value);
  void nextTick(renderGraph);
});

onBeforeUnmount(() => {
  observer?.disconnect();
  disposeChart();
});
</script>

<template>
  <section class="graph-panel" :class="{ 'is-overlay': mode === 'overlay' }" aria-label="语义地图浏览器">
    <aside class="graph-entries" aria-label="语义入口">
      <div class="graph-pane-heading">
        <div class="graph-pane-title">
          <span>浏览入口</span>
          <strong>{{ catalog?.total ?? 0 }} 个{{ activeEntryTypeLabel }}</strong>
        </div>
      </div>

      <div class="entry-type-switch" role="group" aria-label="入口类型">
        <button
          :class="{ active: entryType === 'entity' }"
          type="button"
          @click="emit('updateEntryType', 'entity')"
        >实体</button>
        <button
          :class="{ active: entryType === 'concept' }"
          type="button"
          @click="emit('updateEntryType', 'concept')"
        >属性</button>
      </div>

      <label class="entry-search" for="datalink-entry-search">
        <Search :size="14" aria-hidden="true" />
        <Input
          id="datalink-entry-search"
          :model-value="entryQuery"
          placeholder="搜索名称或别名"
          @update:model-value="emit('updateEntryQuery', String($event))"
        />
      </label>

      <p v-if="loadingCatalog" class="panel-state">正在读取入口...</p>
      <p v-else-if="catalog === null" class="panel-state">选择数据地图后读取可浏览的实体和属性。</p>
      <p v-else-if="catalog.items.length === 0" class="panel-state">没有匹配的{{ activeEntryTypeLabel }}。</p>
      <div v-else class="entry-list">
        <template v-if="showConceptGroups">
          <section v-for="group in conceptGroups" :key="group.key" class="entry-group">
            <button
              v-if="group.entity"
              class="entry-group-label"
              :class="{ active: group.entity.node.id === currentRootNodeId }"
              type="button"
              :title="group.entity.node.name"
              @click="emit('selectEntry', group.entity)"
            >
              <strong>{{ group.entity.node.name }}</strong>
              <span>{{ group.items.length }}</span>
            </button>
            <div v-else class="entry-group-label is-static">
              <strong>未归属</strong>
              <span>{{ group.items.length }}</span>
            </div>
            <button
              v-for="item in group.items"
              :key="`${group.key}:${item.node.id}`"
              class="entry-row"
              :class="{ active: item.node.id === currentRootNodeId }"
              type="button"
              :title="item.node.name"
              @click="emit('selectEntry', item)"
            >
              <strong>{{ item.node.name }}</strong>
              <span>{{ entrySubtitle(item) }}</span>
            </button>
          </section>
        </template>
        <button
          v-else
          v-for="item in catalog.items"
          :key="item.node.id"
          class="entry-row"
          :class="{ active: item.node.id === currentRootNodeId }"
          type="button"
          :title="item.node.name"
          @click="emit('selectEntry', item)"
        >
          <strong>{{ item.node.name }}</strong>
          <span>{{ entrySubtitle(item) }}</span>
        </button>
      </div>
    </aside>

    <section class="graph-canvas-pane" aria-label="语义地图">
      <header class="graph-canvas-heading">
        <div class="graph-pane-title">
          <span>语义地图</span>
          <strong>{{ currentRootName ?? '尚未选择入口' }}</strong>
        </div>
        <div class="graph-canvas-actions">
          <Button
            v-if="allowFullscreen && subgraph && mode === 'embedded'"
            variant="ghost"
            size="icon-sm"
            title="全屏查看"
            @click="emit('fullscreen')"
          >
            <Maximize2 :size="14" aria-hidden="true" /><span class="sr-only">全屏查看</span>
          </Button>
          <Button
            variant="outline"
            size="sm"
            :disabled="loadingSubgraph || !initialRootNodeId || currentRootNodeId === initialRootNodeId"
            @click="emit('resetRoot')"
          >
            <RotateCcw :size="14" />重置
          </Button>
        </div>
      </header>

      <div class="relation-filter" aria-label="关系筛选">
        <div class="relation-filter-heading">
          <span>显示关系</span>
          <span class="relation-filter-hint">{{ showingAllRelations() ? "当前显示全部语义关系" : "仅显示已勾选类型" }}</span>
        </div>
        <div class="relation-options">
          <label class="relation-all">
            <input
              type="checkbox"
              :checked="showingAllRelations()"
              @change="toggleAllEdgeTypes(checkedFromEvent($event))"
            >
            <span>全部</span>
          </label>
          <label v-for="[type, label] in canvasRelationTypes" :key="type">
            <input
              type="checkbox"
              :checked="edgeTypes.includes(type)"
              @change="toggleEdgeType(type, checkedFromEvent($event))"
            >
            <span>{{ label }}</span>
          </label>
        </div>
      </div>

      <p v-if="errorMessage && subgraph === null" class="canvas-state graph-error">{{ errorMessage }}</p>
      <p v-else-if="loadingSubgraph && subgraph === null" class="canvas-state">正在读取语义地图...</p>
      <p v-else-if="subgraph === null" class="canvas-state">从左侧选择一个实体或属性，查看它附近的语义地图。</p>
      <p v-else-if="!semanticMap || semanticMap.nodes.length === 0" class="canvas-state">当前入口没有可显示的实体或属性。</p>
      <template v-else>
        <p v-if="errorMessage" class="graph-error">{{ errorMessage }}</p>
        <p v-else-if="loadingSubgraph" class="graph-loading">正在更新语义地图...</p>
        <div ref="graphElement" class="graph-canvas" aria-label="可缩放、可拖动的语义地图" />
        <div class="graph-canvas-footer">
          <div class="graph-legend" aria-label="节点类型图例">
            <span><i class="legend-entity" />{{ datalinkNodeLabels.entity }}</span>
            <span><i class="legend-concept" />{{ datalinkNodeLabels.concept }}</span>
          </div>
          <span>{{ semanticMap.nodes.length }} 个语义节点，{{ semanticMap.edges.length }} 条语义关系</span>
        </div>
        <div class="relation-legend" aria-label="关系图例">
          <span v-for="item in relationLegend" :key="item.key"><i :class="item.className" />{{ item.label }}</span>
        </div>
        <ul v-if="subgraph.warnings.length" class="graph-warnings">
          <li v-for="warning in subgraph.warnings" :key="warning">{{ warning }}</li>
        </ul>
      </template>
    </section>

    <aside class="graph-inspector" aria-label="实体与属性详情">
      <header class="inspector-header">
        <template v-if="selectedNode">
          <div class="inspector-heading">
            <span class="inspector-kind" :class="`is-${selectedNode.type}`">
              <i class="inspector-kind-mark" :class="`is-${selectedNode.type}`" aria-hidden="true" />
              {{ datalinkNodeLabels[selectedNode.type] }}
            </span>
            <h3 :title="selectedNode.name">{{ selectedNode.name }}</h3>
          </div>
          <span class="inspector-status">已选中</span>
        </template>
        <template v-else-if="selectedEdge">
          <div class="inspector-heading">
            <span class="inspector-kind is-edge">
              <i
                class="inspector-kind-mark"
                :class="selectedEdge.type === 'has_concept' ? 'is-has-concept' : 'is-synonym'"
                aria-hidden="true"
              />
              关系
            </span>
            <h3>{{ datalinkRelationLabels[selectedEdge.type] }}</h3>
          </div>
          <span class="inspector-status">已选中</span>
        </template>
        <template v-else>
          <div class="inspector-heading">
            <span class="inspector-kind">详情</span>
            <h3>未选择</h3>
          </div>
        </template>
      </header>

      <div class="inspector-scroll">
        <template v-if="selectedNode">
          <p
            v-if="selectedNode.description"
            class="inspector-summary"
            :class="`is-${selectedNode.type}`"
          >{{ selectedNode.description }}</p>
          <section v-if="selectedNode.aliases.length" class="inspector-section">
            <header><strong>别名</strong><span>{{ selectedNode.aliases.length }}</span></header>
            <div class="inspector-chips">
              <code v-for="alias in selectedNode.aliases" :key="alias" class="inspector-chip is-alias">{{ alias }}</code>
            </div>
          </section>
          <section v-if="selectedConcepts.length" class="inspector-section">
            <header><strong>关联属性</strong><span>{{ selectedConcepts.length }}</span></header>
            <div class="inspector-chips">
              <button
                v-for="concept in selectedConcepts"
                :key="concept.id"
                type="button"
                class="inspector-chip is-concept"
                :title="concept.name"
                @click="selectInspectorNode(concept.id)"
              >{{ concept.name }}</button>
            </div>
          </section>
          <section v-if="selectedEntities.length" class="inspector-section">
            <header><strong>所属实体</strong><span>{{ selectedEntities.length }}</span></header>
            <div class="inspector-chips">
              <button
                v-for="entity in selectedEntities"
                :key="entity.id"
                type="button"
                class="inspector-chip is-entity"
                :title="entity.name"
                @click="selectInspectorNode(entity.id)"
              >{{ entity.name }}</button>
            </div>
          </section>
          <section class="inspector-section">
            <header><strong>映射字段</strong><span>{{ selectedMappedFieldParts.length }}</span></header>
            <ul v-if="selectedMappedFieldParts.length" class="inspector-fields">
              <li v-for="field in selectedMappedFieldParts" :key="field.ref">
                <code v-if="field.table" class="inspector-field-table">{{ field.table }}</code>
                <span v-if="field.table" class="inspector-field-dot">.</span>
                <code class="inspector-field-name">{{ field.column }}</code>
              </li>
            </ul>
            <p v-else class="inspector-copy">未映射字段</p>
          </section>
        </template>

        <template v-else-if="selectedEdge">
          <p class="inspector-summary is-edge">{{ evidenceDescription(selectedEdge) }}</p>
          <dl class="inspector-facts">
            <div>
              <dt>起点</dt>
              <dd>
                <button
                  v-if="semanticNodeById(selectedEdge.source)"
                  type="button"
                  class="inspector-chip is-endpoint"
                  @click="selectInspectorNode(selectedEdge.source)"
                >{{ edgeEndpoint(selectedEdge, 'source') }}</button>
                <span v-else>{{ edgeEndpoint(selectedEdge, 'source') }}</span>
              </dd>
            </div>
            <div>
              <dt>终点</dt>
              <dd>
                <button
                  v-if="semanticNodeById(selectedEdge.target)"
                  type="button"
                  class="inspector-chip is-endpoint"
                  @click="selectInspectorNode(selectedEdge.target)"
                >{{ edgeEndpoint(selectedEdge, 'target') }}</button>
                <span v-else>{{ edgeEndpoint(selectedEdge, 'target') }}</span>
              </dd>
            </div>
            <div>
              <dt>置信度</dt>
              <dd>
                <span class="inspector-confidence">
                  <strong>{{ confidenceLabel(selectedEdge.confidence) }}</strong>
                  <span
                    v-if="confidenceWidth(selectedEdge.confidence)"
                    class="inspector-confidence-track"
                    aria-hidden="true"
                  ><i :style="{ width: confidenceWidth(selectedEdge.confidence) ?? '0%' }" /></span>
                </span>
              </dd>
            </div>
            <div>
              <dt>来源</dt>
              <dd>{{ datalinkRelationProvenanceLabels[selectedEdge.provenance ?? 'unknown'] }}</dd>
            </div>
            <div v-if="selectedEdge.evidence">
              <dt>{{ selectedEdge.evidence.kind }}</dt>
              <dd>{{ selectedEdge.evidence.summary }}</dd>
            </div>
          </dl>
        </template>

        <div v-else class="inspector-empty">
          <strong>尚未选择节点</strong>
          <span>从左侧选择实体或属性，查看说明和映射字段。</span>
        </div>
      </div>

      <footer class="inspector-footer">
        <Button
          v-if="selectedNode"
          variant="outline"
          size="sm"
          class="inspector-focus"
          :disabled="loadingSubgraph || selectedNode.id === currentRootNodeId"
          @click="emit('focusNode', selectedNode.id)"
        >
          <Focus :size="14" aria-hidden="true" />聚焦此节点
        </Button>
        <dl v-if="subgraph" class="graph-totals">
          <div class="is-version">
            <dt>当前版本</dt>
            <dd :title="graphVersion ?? subgraph.graph_version">{{ graphVersion ?? subgraph.graph_version }}</dd>
          </div>
          <div v-if="semanticMap" class="is-stat"><dt>语义节点</dt><dd>{{ semanticMap.nodes.length }}</dd></div>
          <div v-if="semanticMap" class="is-stat"><dt>语义关系</dt><dd>{{ semanticMap.edges.length }}</dd></div>
        </dl>
      </footer>
    </aside>
  </section>
</template>

<style scoped>
.graph-panel { display: grid; min-width: 0; grid-template-columns: minmax(190px, 0.78fr) minmax(360px, 1.7fr) minmax(210px, 0.9fr); overflow: hidden; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface); }
.graph-panel.is-overlay { height: 100%; min-height: 0; }
.graph-entries, .graph-canvas-pane { display: grid; min-width: 0; align-content: start; gap: 12px; padding: 12px; }
.graph-panel.is-overlay .graph-entries { min-height: 0; overflow: auto; }
.graph-panel.is-overlay .graph-inspector { min-height: 0; overflow: hidden; }
.graph-panel.is-overlay .graph-canvas-pane { min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.graph-entries { border-right: 1px solid var(--workspace-border); background: var(--workspace-surface-subtle); }
.graph-inspector {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border-left: 1px solid var(--workspace-border);
  background: var(--workspace-surface-subtle);
}
.graph-pane-heading, .graph-canvas-heading, .graph-canvas-footer, .relation-filter-heading { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 10px; }
.graph-canvas-heading { align-items: flex-start; }
.graph-pane-title { display: grid; min-width: 0; gap: 2px; }
.graph-pane-heading span, .graph-canvas-heading span, .graph-pane-title span { color: var(--workspace-text-muted); font-size: 10px; }
.graph-pane-heading strong, .graph-canvas-heading strong, .graph-pane-title strong { overflow: hidden; color: var(--workspace-text); text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.entry-type-switch { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-inset); padding: 2px; }
.entry-type-switch button { min-height: 28px; border: 0; border-radius: 3px; background: transparent; color: var(--workspace-text-muted); cursor: pointer; font-size: 11px; font-weight: 600; }
.entry-type-switch button:hover { color: var(--workspace-text); }.entry-type-switch button.active { background: var(--workspace-surface); color: var(--workspace-text); box-shadow: 0 1px 2px rgb(22 31 27 / 9%); }
.entry-search { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 7px; color: var(--workspace-text-muted); }.entry-search :deep(input) { height: 32px; padding-left: 8px; font-size: 11px; }
.entry-list { display: grid; min-height: 0; max-height: 420px; align-content: start; gap: 2px; overflow: auto; padding-right: 2px; }
.entry-group { display: grid; gap: 2px; padding-top: 6px; }
.entry-group:first-child { padding-top: 0; }
.entry-group-label {
  display: flex;
  min-width: 0;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  border: 1px solid transparent;
  border-radius: var(--workspace-radius-sm);
  background: transparent;
  padding: 6px 8px 4px;
  color: var(--workspace-text-muted);
  text-align: left;
}
button.entry-group-label { cursor: pointer; }
button.entry-group-label:hover { color: var(--workspace-text); background: var(--workspace-surface-hover); }
button.entry-group-label.active {
  border-color: color-mix(in srgb, var(--workspace-focus) 45%, var(--workspace-border));
  background: var(--workspace-surface-selected);
  color: var(--workspace-text);
}
.entry-group-label strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; font-weight: 700; letter-spacing: 0.04em; }
.entry-group-label span { flex: none; font-size: 10px; font-variant-numeric: tabular-nums; }
.entry-row { display: grid; min-width: 0; gap: 3px; border: 1px solid transparent; border-radius: var(--workspace-radius-sm); background: transparent; padding: 8px; color: var(--workspace-text); cursor: pointer; text-align: left; }.entry-row:hover { background: var(--workspace-surface-hover); }.entry-row.active { border-color: color-mix(in srgb, var(--workspace-focus) 45%, var(--workspace-border)); background: var(--workspace-surface-selected); }.entry-row strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }.entry-row span { display: -webkit-box; overflow: hidden; color: var(--workspace-text-muted); font-size: 10px; line-height: 1.4; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; }
.panel-state, .canvas-state { margin: 0; color: var(--workspace-text-muted); font-size: 12px; line-height: 1.5; }.canvas-state { display: grid; min-height: 260px; height: min(300px, max(260px, calc(100dvh - 560px))); place-items: center; padding: 24px; text-align: center; }.graph-error { margin: 0; border-left: 2px solid var(--workspace-state-error); padding-left: 8px; color: var(--workspace-state-error); font-size: 11px; line-height: 1.5; }.canvas-state.graph-error { color: var(--workspace-state-error); }.graph-loading { margin: 0; color: var(--workspace-text-muted); font-size: 11px; }
.graph-canvas-pane { min-width: 0; overflow: hidden; background: var(--workspace-surface); }
.graph-canvas-heading :deep(button) { flex: 0 0 auto; }
.graph-canvas-actions { display: flex; flex: 0 0 auto; flex-wrap: nowrap; align-items: center; gap: 6px; }
.relation-filter { display: grid; min-width: 0; gap: 7px; border-top: 1px solid var(--workspace-border); border-bottom: 1px solid var(--workspace-border); padding: 8px 0; }
.relation-filter-heading { flex-wrap: wrap; color: var(--workspace-text-muted); font-size: 10px; font-weight: 700; }
.relation-filter-hint { font-weight: 500; }
.relation-options { display: flex; flex-wrap: wrap; gap: 6px 10px; min-width: 0; }
.relation-options label { display: inline-flex; flex: 0 0 auto; max-width: 100%; align-items: center; gap: 4px; color: var(--workspace-text-muted); cursor: pointer; font-size: 10px; white-space: nowrap; }
.relation-options .relation-all { color: var(--workspace-text); font-weight: 700; }
.relation-options input { width: 13px; height: 13px; flex: 0 0 auto; accent-color: var(--workspace-focus); }
.graph-canvas {
  width: 100%;
  min-height: 260px;
  height: min(300px, max(260px, calc(100dvh - 560px)));
  border: 1px solid var(--workspace-border);
  border-radius: var(--workspace-radius-sm);
  background:
    radial-gradient(ellipse 70% 55% at 18% 12%, var(--workspace-page-glow-a), transparent 58%),
    radial-gradient(ellipse 55% 50% at 88% 86%, var(--workspace-page-glow-b), transparent 62%),
    var(--workspace-surface-inset);
  cursor: grab;
  touch-action: none;
  overscroll-behavior: none;
  user-select: none;
}
.graph-canvas:active { cursor: grabbing; }
.graph-panel.is-overlay .graph-canvas { flex: 1 1 0; min-height: 0; height: auto; }
.graph-canvas-footer { align-items: flex-start; color: var(--workspace-text-muted); font-size: 10px; }
.graph-legend, .relation-legend { display: flex; flex-wrap: wrap; gap: 5px 8px; }
.graph-legend span, .relation-legend span { display: inline-flex; align-items: center; gap: 4px; }
.graph-legend i.legend-entity {
  width: 18px;
  height: 10px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--workspace-tool-datalink) 22%, transparent);
  border: 1px solid var(--workspace-tool-datalink);
}
.graph-legend i.legend-concept {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--workspace-tool-sql);
}
.relation-legend { color: var(--workspace-text-muted); font-size: 10px; }
.relation-legend i { display: inline-block; width: 18px; border-top: 1px dotted var(--workspace-text-muted); }
.relation-legend i.has-concept { border-top: 2px solid color-mix(in srgb, var(--workspace-tool-datalink) 70%, transparent); }
.relation-legend i.synonym { border-top: 1.5px dashed var(--workspace-text-muted); }
.graph-warnings { display: grid; gap: 3px; margin: 0; padding: 0 0 0 17px; color: var(--workspace-state-warning); font-size: 11px; line-height: 1.45; }
.inspector-header {
  display: flex;
  flex: none;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  border-bottom: 1px solid var(--workspace-border);
  padding: 12px;
}
.inspector-heading { display: grid; min-width: 0; gap: 4px; }
.inspector-heading h3 { margin: 0; overflow-wrap: anywhere; color: var(--workspace-text); font-size: 14px; line-height: 1.35; }
.inspector-kind {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--workspace-text-muted);
  font-size: 10px;
  font-weight: 600;
}
.inspector-kind.is-entity { color: var(--workspace-tool-datalink); }
.inspector-kind.is-concept { color: var(--workspace-tool-sql); }
.inspector-kind-mark.is-entity {
  width: 18px;
  height: 10px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--workspace-tool-datalink) 22%, transparent);
  border: 1px solid var(--workspace-tool-datalink);
}
.inspector-kind-mark.is-concept {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--workspace-tool-sql);
}
.inspector-kind-mark.is-has-concept {
  width: 16px;
  border-top: 2px solid color-mix(in srgb, var(--workspace-tool-datalink) 70%, transparent);
}
.inspector-kind-mark.is-synonym {
  width: 16px;
  border-top: 1.5px dashed var(--workspace-text-muted);
}
.inspector-status { flex: none; color: var(--workspace-text-muted); font-size: 10px; white-space: nowrap; }
.inspector-scroll { display: grid; min-height: 0; flex: 1; align-content: start; gap: 13px; overflow: auto; padding: 12px; }
.inspector-summary {
  margin: 0;
  border-left: 2px solid var(--workspace-tool-datalink);
  border-radius: var(--workspace-radius-sm);
  background: var(--workspace-surface);
  padding: 8px 9px;
  color: var(--workspace-text);
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.inspector-summary.is-concept { border-left-color: var(--workspace-tool-sql); }
.inspector-summary.is-edge { border-left-color: var(--workspace-border-strong); color: var(--workspace-text-muted); }
.inspector-section { display: grid; gap: 7px; }
.inspector-section header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.inspector-section header strong { color: var(--workspace-text); font-size: 11px; }
.inspector-section header span { color: var(--workspace-text-muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.inspector-chips { display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; }
.inspector-chip {
  display: inline-flex;
  max-width: 100%;
  align-items: center;
  overflow: hidden;
  border: 1px solid var(--workspace-border);
  border-radius: 999px;
  background: var(--workspace-surface);
  padding: 3px 8px;
  color: var(--workspace-text);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
button.inspector-chip { cursor: pointer; }
button.inspector-chip:hover { background: var(--workspace-surface-hover); }
.inspector-chip.is-concept {
  border-color: color-mix(in srgb, var(--workspace-tool-sql) 45%, var(--workspace-border));
  color: var(--workspace-tool-sql);
}
.inspector-chip.is-entity {
  border-color: color-mix(in srgb, var(--workspace-tool-datalink) 45%, var(--workspace-border));
  color: var(--workspace-tool-datalink);
}
.inspector-chip.is-alias {
  color: var(--workspace-text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.inspector-copy { margin: 0; color: var(--workspace-text-muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.inspector-fields { display: grid; gap: 5px; margin: 0; padding: 0; list-style: none; }
.inspector-fields li {
  display: flex;
  min-width: 0;
  align-items: baseline;
  overflow-wrap: anywhere;
  border: 1px solid var(--workspace-code-border);
  border-radius: var(--workspace-radius-sm);
  background: var(--workspace-surface-inset);
  padding: 6px 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
}
.inspector-field-table { color: var(--workspace-text-muted); }
.inspector-field-dot { color: var(--workspace-text-subtle); }
.inspector-field-name { color: var(--workspace-text); }
.inspector-facts { display: grid; gap: 8px; margin: 0; }
.inspector-facts div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 7px; min-width: 0; }
.inspector-facts dt { color: var(--workspace-text-muted); font-size: 10px; }
.inspector-facts dd { min-width: 0; margin: 0; color: var(--workspace-text); font-size: 11px; overflow-wrap: anywhere; }
.inspector-confidence { display: grid; gap: 4px; min-width: 0; }
.inspector-confidence strong { font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; }
.inspector-confidence-track {
  display: block;
  height: 3px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--workspace-border);
}
.inspector-confidence-track i {
  display: block;
  height: 100%;
  background: var(--workspace-tool-datalink);
}
.inspector-empty {
  display: grid;
  min-height: 140px;
  place-items: center;
  align-content: center;
  gap: 6px;
  padding: 16px 8px;
  text-align: center;
}
.inspector-empty strong { color: var(--workspace-text); font-size: 12px; }
.inspector-empty span { max-width: 220px; color: var(--workspace-text-muted); font-size: 10px; line-height: 1.5; }
.inspector-footer {
  display: grid;
  flex: none;
  gap: 10px;
  border-top: 1px solid var(--workspace-border);
  background: var(--workspace-surface);
  padding: 10px 12px;
}
.inspector-focus { width: 100%; }
.graph-totals { display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 0; }
.graph-totals .is-version { min-width: 0; flex: 1 1 100%; }
.graph-totals .is-stat { flex: 0 0 auto; }
.graph-totals dt { color: var(--workspace-text-subtle); font-size: 9px; }
.graph-totals dd { min-width: 0; margin: 0; color: var(--workspace-text-muted); font-size: 10px; }
.graph-totals .is-version dd {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.graph-totals .is-stat dd { color: var(--workspace-text); font-variant-numeric: tabular-nums; font-weight: 600; }
@media (max-width: 1080px) {
  .graph-panel { grid-template-columns: minmax(190px, 0.75fr) minmax(360px, 1.6fr); }
  .graph-inspector { grid-column: 1 / -1; border-top: 1px solid var(--workspace-border); border-left: 0; }
}
@media (max-width: 860px) {
  .graph-panel.is-overlay {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    grid-template-areas: "canvas canvas" "entries inspector";
    grid-template-rows: minmax(0, 1fr) minmax(132px, 30dvh);
  }
  .graph-panel.is-overlay .graph-entries {
    grid-area: entries;
    min-height: 0;
    max-height: 30dvh;
    overflow: auto;
    border-right: 1px solid var(--workspace-border);
    border-bottom: 0;
  }
  .graph-panel.is-overlay .graph-canvas-pane { grid-area: canvas; min-width: 0; }
  .graph-panel.is-overlay .graph-inspector {
    grid-area: inspector;
    grid-column: auto;
    min-height: 0;
    max-height: 30dvh;
    overflow: hidden;
    border-top: 0;
    border-left: 0;
  }
  .graph-panel.is-overlay .inspector-header,
  .graph-panel.is-overlay .inspector-footer { padding: 8px 10px; }
  .graph-panel.is-overlay .inspector-scroll { padding: 8px 10px; gap: 10px; }
  .graph-panel.is-overlay .entry-list { max-height: none; }
  .graph-panel.is-overlay .graph-canvas { min-height: 0; height: auto; }
}
@media (max-width: 860px) and (max-height: 740px) {
  .graph-panel.is-overlay {
    grid-template-rows: minmax(0, 1fr) minmax(108px, 22dvh);
  }
  .graph-panel.is-overlay .graph-entries,
  .graph-panel.is-overlay .graph-inspector { max-height: 22dvh; }
}
@media (max-width: 720px) {
  .graph-panel:not(.is-overlay) {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "canvas" "entries" "inspector";
  }
  .graph-panel:not(.is-overlay) .graph-entries { grid-area: entries; border: 0; border-top: 1px solid var(--workspace-border); }
  .graph-panel:not(.is-overlay) .graph-canvas-pane { grid-area: canvas; min-width: 0; }
  .graph-panel:not(.is-overlay) .graph-inspector {
    grid-area: inspector;
    border-top: 1px solid var(--workspace-border);
  }
  .graph-canvas-heading, .relation-filter, .graph-canvas, .graph-canvas-footer { width: 100%; min-width: 0; }
  .entry-list { max-height: 140px; }
  .graph-panel:not(.is-overlay) .graph-canvas,
  .graph-panel:not(.is-overlay) .canvas-state {
    min-height: min(48dvh, 420px);
    height: min(48dvh, 420px);
  }
  .graph-canvas-footer { flex-direction: column; gap: 5px; }
}
</style>
