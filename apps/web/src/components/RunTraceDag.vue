<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";
import { Bot, ChevronRight, FileText, Flag, Focus, GitBranch, Layers, List, Maximize2, MessageSquare, Play, Wrench, ZoomIn, ZoomOut } from "@lucide/vue";

import type { TraceDag, TraceDagEdge, TraceDagNode, TraceDagSection } from "@/api/types";
import { Button } from "@/components/ui/button";
import { traceActionLabel, traceNodeLabel, traceSectionLabel } from "@/lib/traceDisplay";

type ViewTransform = { x: number; y: number; scale: number };
type PointerDrag = { pointerId: number; startX: number; startY: number; view: ViewTransform };
type DisplayNode = TraceDagNode & { section?: TraceDagSection };
type LayoutNode = DisplayNode & { x: number; y: number; rank: number };
type LayoutEdge = TraceDagEdge & { path: string };
type DagLayout = { nodes: LayoutNode[]; edges: LayoutEdge[]; width: number; height: number };
type StageItem = { type: "section"; section: TraceDagSection; nodes: TraceDagNode[] }
  | { type: "node"; node: TraceDagNode };

const props = withDefaults(defineProps<{
  dag: TraceDag | null;
  loading?: boolean;
  error?: string | null;
  selectedNodeId?: string;
  mode?: "embedded" | "overlay";
  allowFullscreen?: boolean;
  showActionRecords?: boolean;
  nodeSources?: Readonly<Record<string, string>>;
}>(), {
  loading: false,
  error: null,
  selectedNodeId: "",
  mode: "embedded",
  allowFullscreen: false,
  showActionRecords: true,
  nodeSources: () => ({}),
});

const emit = defineEmits<{
  selectNode: [node: TraceDagNode];
  selectSection: [section: TraceDagSection];
  fullscreen: [];
}>();

const WIDE_CANVAS_WIDTH = 980;
const WIDE_CANVAS_HEIGHT = 560;
const OVERLAY_CANVAS_WIDTH = 760;
const COMPACT_CANVAS_WIDTH = 520;
const COMPACT_CANVAS_HEIGHT = 620;
const LEVEL_GAP = 92;
const OVERLAY_LEVEL_GAP = 52;
const SIBLING_GAP = 190;
const MARGIN_X = 78;
const MARGIN_Y = 64;
const MIN_SCALE = 0.42;
const MAX_SCALE = 2.2;
const DOT_RADIUS = 9;
const PILL_WIDTH = 166;
const PILL_HEIGHT = 34;
const PILL_LABEL_X = -42;

const collapsedSectionIds = ref<Set<string>>(new Set());
const expandedStageIds = ref<Set<string>>(new Set());
const activeSectionId = ref("");
const embeddedView = ref<"stages" | "graph">("stages");
const viewModeId = useId();
const showGraph = computed(() => props.mode === "overlay" || embeddedView.value === "graph");
const view = ref<ViewTransform>({ x: 0, y: 0, scale: 1 });
const previousLayoutKey = ref("");
const dragState = ref<PointerDrag | null>(null);
const svgRef = ref<SVGSVGElement | null>(null);
const measuredCanvas = ref<{ width: number; height: number } | null>(null);
const compactViewport = ref(false);
let pointerSelectedNode: { id: string; at: number } | null = null;
let compactMediaQuery: MediaQueryList | null = null;

const nodes = computed(() => props.dag?.nodes ?? []);
const sections = computed(() => props.dag?.sections ?? []);
const stageItems = computed<StageItem[]>(() => {
  const byId = new Map(nodes.value.map((node) => [node.id, node]));
  const groupedIds = new Set(sections.value.flatMap((section) => section.node_ids));
  const items: StageItem[] = sections.value.map((section) => ({
    type: "section",
    section,
    nodes: section.node_ids.map((id) => byId.get(id)).filter((node): node is TraceDagNode => node !== undefined),
  }));
  items.push(...nodes.value.filter((node) => !groupedIds.has(node.id)).map((node) => ({ type: "node" as const, node })));
  const seq = (item: StageItem) => item.type === "section" ? item.section.start_seq : item.node.start_seq ?? Number.MAX_SAFE_INTEGER;
  return items.sort((left, right) => seq(left) - seq(right));
});
const compactCanvas = computed(() => props.mode === "embedded" || compactViewport.value);
const canvasWidth = computed(() => measuredCanvas.value?.width ?? (compactCanvas.value
  ? COMPACT_CANVAS_WIDTH
  : props.mode === "overlay" ? OVERLAY_CANVAS_WIDTH : WIDE_CANVAS_WIDTH));
const canvasHeight = computed(() => measuredCanvas.value?.height ?? (compactCanvas.value ? COMPACT_CANVAS_HEIGHT : WIDE_CANVAS_HEIGHT));
const selectedNode = computed(() => nodes.value.find((node) => node.id === props.selectedNodeId) ?? null);
const orderedActions = computed(() => [...(selectedNode.value?.action_records ?? [])].sort(
  (left, right) => left.event_seq - right.event_seq || left.id.localeCompare(right.id),
));
const displayDag = computed(() => collapseSections(props.dag, sections.value, collapsedSectionIds.value));
const layout = computed(() => buildLayout(
  displayDag.value,
  canvasWidth.value,
  canvasHeight.value,
  props.mode === "overlay" ? OVERLAY_LEVEL_GAP : LEVEL_GAP,
));
const minimumScale = computed(() => Math.min(MIN_SCALE, fitLayout(layout.value, canvasWidth.value, canvasHeight.value).scale));
const layoutKey = computed(() => `${canvasWidth.value}x${canvasHeight.value}:${layout.value?.nodes.map((node) => node.id).join("|") ?? ""}`);
const markerId = computed(() => `trace-dag-arrow-${props.mode}`);

watch(svgRef, (element, _previous, onCleanup) => {
  measuredCanvas.value = null;
  if (!element || typeof ResizeObserver !== "function") return;
  const measure = () => {
    const { width, height } = element.getBoundingClientRect();
    if (width > 0 && height > 0 && (width !== measuredCanvas.value?.width || height !== measuredCanvas.value?.height)) {
      measuredCanvas.value = { width, height };
    }
  };
  const observer = new ResizeObserver(measure);
  observer.observe(element);
  measure();
  onCleanup(() => observer.disconnect());
}, { flush: "post" });

watch(layoutKey, (next) => {
  if (next === previousLayoutKey.value) return;
  previousLayoutKey.value = next;
  view.value = fitLayout(layout.value, canvasWidth.value, canvasHeight.value);
  if (activeSectionId.value) focusNodes(layout.value?.nodes.filter(inActiveSection) ?? []);
}, { immediate: true });

watch(() => props.dag?.run_id, () => {
  activeSectionId.value = "";
  collapsedSectionIds.value = initialCollapsedSectionIds(props.dag);
  expandedStageIds.value = new Set(sections.value.filter((section) => !isFinishedStatus(section.status)).map((section) => section.id));
  previousLayoutKey.value = "";
}, { immediate: true });

watch([() => props.selectedNodeId, sections], () => {
  const selectedSections = sections.value.filter((section) => section.node_ids.includes(props.selectedNodeId));
  if (selectedSections.length) expandedStageIds.value = new Set([...expandedStageIds.value, ...selectedSections.map((section) => section.id)]);
  collapsedSectionIds.value = new Set([...collapsedSectionIds.value].filter((id) => !selectedSections.some((section) => section.id === id)));
  if (props.selectedNodeId) {
    activeSectionId.value = "";
    void nextTick(() => focusNodes(layout.value?.nodes.filter((node) => node.id === props.selectedNodeId) ?? []));
  }
}, { immediate: true });

function initialCollapsedSectionIds(dag: TraceDag | null | undefined): Set<string> {
  if (!dag) return new Set();
  return new Set(
    dag.sections
      .filter((section) => isFinishedStatus(section.status))
      .map((section) => section.id),
  );
}

function isFinishedStatus(status: string | null): boolean {
  return status === "succeeded"
    || status === "completed"
    || status === "failed"
    || status === "canceled"
    || status === "cancelled";
}

function statusLabel(status: string | null): string {
  if (status === "succeeded" || status === "completed") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "canceled" || status === "cancelled") return "已取消";
  if (status === "running") return "进行中";
  if (status === "queued") return "等待中";
  return status ?? "未记录";
}

function nodeText(node: DisplayNode): string {
  return node.section ? traceSectionLabel(node.section, nodes.value) : traceNodeLabel(node);
}

function nodeDisplayText(node: DisplayNode): string {
  const text = nodeText(node);
  if (text.length <= 10) return text;
  return `${text.slice(0, 8)}...`;
}

function nodeIcon(node: TraceDagNode) {
  return { "run-start": Play, preparation: Layers, "agent-turn": Bot, tool: Wrench, artifact: FileText, "final-answer": MessageSquare, "run-terminal": Flag }[node.kind];
}

function toggleStage(sectionId: string): void {
  const next = new Set(expandedStageIds.value);
  if (next.has(sectionId)) next.delete(sectionId);
  else next.add(sectionId);
  expandedStageIds.value = next;
  const collapsed = new Set(collapsedSectionIds.value);
  if (next.has(sectionId)) collapsed.delete(sectionId);
  else collapsed.add(sectionId);
  collapsedSectionIds.value = collapsed;
}

function focusNodes(targets: readonly LayoutNode[]): void {
  if (!targets.length) return;
  const left = Math.min(...targets.map((node) => node.x)) - 90;
  const right = Math.max(...targets.map((node) => node.x)) + 110;
  const top = Math.min(...targets.map((node) => node.y)) - 40;
  const bottom = Math.max(...targets.map((node) => node.y)) + 50;
  const scale = Math.max(minimumScale.value, Math.min(1.12, (canvasWidth.value - 48) / (right - left), (canvasHeight.value - 48) / (bottom - top)));
  view.value = { scale, x: canvasWidth.value / 2 - (left + right) / 2 * scale, y: canvasHeight.value / 2 - (top + bottom) / 2 * scale };
}

function inActiveSection(node: DisplayNode): boolean {
  const section = sections.value.find((item) => item.id === activeSectionId.value);
  return !!section && (node.section?.id === section.id || section.node_ids.includes(node.id));
}

function nodeIsDot(node: DisplayNode): boolean {
  return !node.section && (node.kind === "tool" || node.kind === "artifact");
}

function nodeTone(node: DisplayNode): string {
  if (node.kind === "tool") return "tool";
  if (node.kind === "artifact") return "artifact";
  if (node.kind === "run-terminal" || node.status === "failed") return "terminal";
  if (node.kind === "final-answer") return "answer";
  return "context";
}

function toggleSection(sectionId: string): void {
  const next = new Set(collapsedSectionIds.value);
  if (next.has(sectionId)) next.delete(sectionId);
  else next.add(sectionId);
  collapsedSectionIds.value = next;
  expandedStageIds.value = new Set(sections.value.filter((section) => !next.has(section.id)).map((section) => section.id));
  activeSectionId.value = sectionId;
  const section = sections.value.find((item) => item.id === sectionId);
  if (section) emit("selectSection", section);
  void nextTick(() => focusNodes(layout.value?.nodes.filter(inActiveSection) ?? []));
}

function selectDisplayNode(node: DisplayNode): void {
  if (node.section) {
    toggleSection(node.section.id);
    return;
  }
  activeSectionId.value = "";
  emit("selectNode", node);
}

function selectPointerNode(node: DisplayNode, event: PointerEvent): void {
  if (event.button !== 0) return;
  pointerSelectedNode = { id: node.id, at: performance.now() };
  selectDisplayNode(node);
}

function selectClickNode(node: DisplayNode, event: MouseEvent): void {
  if (
    pointerSelectedNode?.id === node.id
    && performance.now() - pointerSelectedNode.at < 500
  ) {
    pointerSelectedNode = null;
    return;
  }
  selectDisplayNode(node);
}

function zoomAt(current: ViewTransform, factor: number, x: number, y: number): ViewTransform {
  const scale = Math.min(MAX_SCALE, Math.max(minimumScale.value, current.scale * factor));
  const ratio = scale / current.scale;
  return { scale, x: x - (x - current.x) * ratio, y: y - (y - current.y) * ratio };
}

function zoomBy(factor: number): void {
  view.value = zoomAt(view.value, factor, canvasWidth.value / 2, canvasHeight.value / 2);
}

function resetView(): void {
  view.value = fitLayout(layout.value, canvasWidth.value, canvasHeight.value);
}

function handleWheel(event: WheelEvent): void {
  event.preventDefault();
  const rect = svgRef.value?.getBoundingClientRect();
  if (!rect) return;
  const x = ((event.clientX - rect.left) / rect.width) * canvasWidth.value;
  const y = ((event.clientY - rect.top) / rect.height) * canvasHeight.value;
  view.value = zoomAt(view.value, event.deltaY > 0 ? 0.9 : 1.1, x, y);
}

function handlePointerDown(event: PointerEvent): void {
  if (event.button !== 0) return;
  const target = event.target as Element | null;
  if (target?.closest("[data-trace-dag-node]")) return;
  dragState.value = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    view: view.value,
  };
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
}

function handlePointerMove(event: PointerEvent): void {
  const drag = dragState.value;
  if (!drag || drag.pointerId !== event.pointerId) return;
  view.value = {
    ...drag.view,
    x: drag.view.x + event.clientX - drag.startX,
    y: drag.view.y + event.clientY - drag.startY,
  };
}

function handlePointerUp(event: PointerEvent): void {
  if (!dragState.value || dragState.value.pointerId !== event.pointerId) return;
  dragState.value = null;
  (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
}

function edgeSelected(edge: LayoutEdge): boolean {
  if (activeSectionId.value) {
    const ids = new Set(layout.value?.nodes.filter(inActiveSection).map((node) => node.id));
    return ids.has(edge.source) || ids.has(edge.target);
  }
  return edge.source === props.selectedNodeId || edge.target === props.selectedNodeId;
}

function nodePosition(node: LayoutNode): string {
  return `translate(${node.x} ${node.y})`;
}

function actionStatusClass(status: string | null): string {
  if (status === "succeeded" || status === "completed") return "trace-action-success";
  if (status === "failed") return "trace-action-error";
  if (status === "running" || status === "queued") return "trace-action-running";
  return "";
}

function isFailureStatus(status: string | null): boolean {
  return status === "failed" || status === "canceled" || status === "cancelled";
}

function actionReasonLabel(action: DisplayNode["action_records"][number]): string | null {
  if (action.reason) return `原因：${action.reason}`;
  return isFailureStatus(action.status) ? "失败原因未提供" : null;
}

function collapseSections(
  dag: TraceDag | null,
  sections: TraceDagSection[],
  collapsed: ReadonlySet<string>,
): TraceDag | null {
  if (!dag) return null;
  const hidden = new Map<string, DisplayNode>();
  const collapsedNodes: DisplayNode[] = [];
  for (const section of sections) {
    if (!collapsed.has(section.id)) continue;
    const source = dag.nodes.find((node) => section.node_ids.includes(node.id));
    if (!source) continue;
    const pseudo: DisplayNode = {
      ...source,
      id: `section:${section.id}`,
      label: section.title,
      status: section.status,
      start_seq: section.start_seq,
      end_seq: section.end_seq,
      section,
      summary: `${section.node_ids.length} 个节点`,
    };
    collapsedNodes.push(pseudo);
    for (const nodeId of section.node_ids) hidden.set(nodeId, pseudo);
  }
  const visibleNodes = dag.nodes.filter((node) => !hidden.has(node.id));
  const edgeById = new Map<string, TraceDagEdge>();
  for (const edge of dag.edges) {
    const source = hidden.get(edge.source)?.id ?? edge.source;
    const target = hidden.get(edge.target)?.id ?? edge.target;
    if (source === target) continue;
    const id = `${source}->${target}:${edge.kind}`;
    if (!edgeById.has(id)) edgeById.set(id, { ...edge, id, source, target });
  }
  return { ...dag, nodes: [...visibleNodes, ...collapsedNodes], edges: [...edgeById.values()] };
}

function buildLayout(
  dag: TraceDag | null,
  viewportWidth: number,
  viewportHeight: number,
  levelGap: number,
): DagLayout | null {
  if (!dag || dag.nodes.length === 0) return null;
  const nodes = [...dag.nodes].sort((left, right) => (left.start_seq ?? 2 ** 31) - (right.start_seq ?? 2 ** 31) || left.id.localeCompare(right.id));
  const rank = buildNodeRanks(nodes, dag.edges);
  const groups = new Map<number, DisplayNode[]>();
  for (const node of nodes) {
    const nodeRank = rank.get(node.id) ?? 0;
    const group = groups.get(nodeRank) ?? [];
    group.push(node);
    groups.set(nodeRank, group);
  }
  const maxCount = Math.max(...[...groups.values()].map((group) => group.length), 1);
  const width = Math.max(viewportWidth, MARGIN_X * 2 + (maxCount - 1) * SIBLING_GAP + 180);
  const layoutNodes: LayoutNode[] = [];
  for (const [nodeRank, group] of [...groups.entries()].sort((left, right) => left[0] - right[0])) {
    const sorted = [...group].sort((left, right) => (left.start_seq ?? 2 ** 31) - (right.start_seq ?? 2 ** 31) || left.id.localeCompare(right.id));
    const center = width / 2;
    const startX = center - ((sorted.length - 1) * SIBLING_GAP) / 2;
    sorted.forEach((node, index) => layoutNodes.push({ ...node, x: startX + index * SIBLING_GAP, y: MARGIN_Y + nodeRank * levelGap, rank: nodeRank }));
  }
  const positions = new Map(layoutNodes.map((node) => [node.id, node]));
  const edges = dag.edges.flatMap((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) return [];
    return [{ ...edge, path: edgePath(source, target) }];
  });
  const maxRank = Math.max(...layoutNodes.map((node) => node.rank), 0);
  return { nodes: layoutNodes, edges, width, height: Math.max(viewportHeight, MARGIN_Y * 2 + maxRank * levelGap + 100) };
}

function buildNodeRanks(nodes: readonly TraceDagNode[], edges: readonly TraceDagEdge[]): Map<string, number> {
  const outgoing = new Map(nodes.map((node) => [node.id, new Set<string>()]));
  for (const edge of edges) {
    if (outgoing.has(edge.target)) outgoing.get(edge.source)?.add(edge.target);
  }

  // Collapsing noncontiguous sections can introduce cycles. Condense them only
  // for ranking; the layout still renders every display node and relationship.
  const indices = new Map<string, number>();
  const lowLinks = new Map<string, number>();
  const active: string[] = [];
  const onStack = new Set<string>();
  const componentByNode = new Map<string, number>();
  let componentCount = 0;

  function visit(id: string): void {
    const index = indices.size;
    indices.set(id, index);
    lowLinks.set(id, index);
    active.push(id);
    onStack.add(id);
    for (const target of outgoing.get(id) ?? []) {
      if (!indices.has(target)) {
        visit(target);
        lowLinks.set(id, Math.min(lowLinks.get(id)!, lowLinks.get(target)!));
      } else if (onStack.has(target)) {
        lowLinks.set(id, Math.min(lowLinks.get(id)!, indices.get(target)!));
      }
    }
    if (lowLinks.get(id) !== index) return;
    let member: string | undefined;
    do {
      member = active.pop()!;
      onStack.delete(member);
      componentByNode.set(member, componentCount);
    } while (member !== id);
    componentCount += 1;
  }

  for (const node of nodes) {
    if (!indices.has(node.id)) visit(node.id);
  }

  const components = Array.from({ length: componentCount }, () => ({ rank: 0, incoming: 0, outgoing: new Set<number>() }));
  for (const [sourceId, targets] of outgoing) {
    const sourceIndex = componentByNode.get(sourceId)!;
    const source = components[sourceIndex]!;
    for (const targetId of targets) {
      const targetIndex = componentByNode.get(targetId)!;
      if (sourceIndex === targetIndex || source.outgoing.has(targetIndex)) continue;
      source.outgoing.add(targetIndex);
      components[targetIndex]!.incoming += 1;
    }
  }
  const ready = components.flatMap((component, index) => component.incoming === 0 ? [index] : []);
  for (let cursor = 0; cursor < ready.length; cursor += 1) {
    const source = components[ready[cursor]!]!;
    for (const targetIndex of source.outgoing) {
      const target = components[targetIndex]!;
      target.rank = Math.max(target.rank, source.rank + 1);
      target.incoming -= 1;
      if (target.incoming === 0) ready.push(targetIndex);
    }
  }
  return new Map(nodes.map((node) => [node.id, components[componentByNode.get(node.id)!]!.rank]));
}

function edgePath(source: LayoutNode, target: LayoutNode): string {
  const sourceGap = source.kind === "tool" || source.kind === "artifact" ? DOT_RADIUS + 4 : PILL_HEIGHT / 2 + 3;
  const targetGap = target.kind === "tool" || target.kind === "artifact" ? DOT_RADIUS + 4 : PILL_HEIGHT / 2 + 3;
  const sourceY = source.y + sourceGap;
  const targetY = target.y - targetGap;
  const middle = sourceY + (targetY - sourceY) / 2;
  return `M ${source.x} ${sourceY} C ${source.x} ${middle}, ${target.x} ${middle}, ${target.x} ${targetY}`;
}

function fitLayout(
  layout: DagLayout | null,
  viewportWidth: number,
  viewportHeight: number,
): ViewTransform {
  if (!layout) return { x: 0, y: 0, scale: 1 };
  const scale = Math.min(1.12, (viewportWidth - 64) / layout.width, (viewportHeight - 64) / layout.height);
  return { scale, x: (viewportWidth - layout.width * scale) / 2, y: 28 };
}

function syncCompactViewport(event?: MediaQueryListEvent): void {
  compactViewport.value = event?.matches ?? compactMediaQuery?.matches ?? false;
}

onMounted(() => {
  if (typeof window.matchMedia !== "function") return;
  compactMediaQuery = window.matchMedia("(max-width: 640px)");
  syncCompactViewport();
  compactMediaQuery.addEventListener("change", syncCompactViewport);
});

onBeforeUnmount(() => compactMediaQuery?.removeEventListener("change", syncCompactViewport));
</script>

<template>
  <section class="trace-dag" :class="`trace-dag-${props.mode}`" aria-label="运行 Trace DAG">
    <header class="trace-dag-toolbar">
      <fieldset v-if="props.mode === 'embedded'" class="trace-view-mode" aria-label="执行记录视图">
        <label :class="{ active: embeddedView === 'stages' }">
          <input v-model="embeddedView" type="radio" :name="viewModeId" value="stages" />
          <List :size="14" aria-hidden="true" />阶段
        </label>
        <label :class="{ active: embeddedView === 'graph' }">
          <input v-model="embeddedView" type="radio" :name="viewModeId" value="graph" />
          <GitBranch :size="14" aria-hidden="true" />关系图
        </label>
      </fieldset>
      <div v-else class="trace-dag-heading">
        <strong>执行关系</strong>
        <span v-if="dag">{{ dag.nodes.length }} 节点 · {{ dag.edges.length }} 条关系</span>
      </div>
      <div class="trace-dag-tools">
        <Button v-if="showGraph" variant="ghost" size="icon-sm" title="缩小" :disabled="view.scale <= minimumScale" @click="zoomBy(.85)">
          <ZoomOut :size="14" aria-hidden="true" /><span class="sr-only">缩小</span>
        </Button>
        <Button v-if="showGraph" variant="ghost" size="icon-sm" title="适合画布" @click="resetView">
          <Focus :size="14" aria-hidden="true" /><span class="sr-only">适合画布</span>
        </Button>
        <Button v-if="showGraph" variant="ghost" size="icon-sm" title="放大" :disabled="view.scale >= MAX_SCALE" @click="zoomBy(1.18)">
          <ZoomIn :size="14" aria-hidden="true" /><span class="sr-only">放大</span>
        </Button>
        <Button v-if="props.allowFullscreen" variant="ghost" size="icon-sm" title="全屏查看" @click="emit('fullscreen')">
          <Maximize2 :size="14" aria-hidden="true" /><span class="sr-only">全屏查看</span>
        </Button>
      </div>
    </header>

    <div v-if="loading && !dag" class="trace-dag-state" aria-live="polite">正在读取执行关系…</div>
    <div v-else-if="error && !dag" class="trace-dag-state trace-dag-error" role="alert">{{ error }}</div>
    <div v-else-if="!dag || dag.nodes.length === 0" class="trace-dag-state">当前 Run 尚无可构造的 Trace。</div>

    <template v-else>
      <ol v-if="!showGraph" class="trace-stage-list" aria-label="执行阶段">
        <li v-for="(item, index) in stageItems" :key="item.type === 'section' ? item.section.id : item.node.id">
          <template v-if="item.type === 'section'">
            <button
              type="button"
              class="trace-stage-heading"
              :aria-expanded="expandedStageIds.has(item.section.id)"
              :aria-controls="`${viewModeId}-stage-${index}`"
              :title="item.section.title"
              @click="toggleStage(item.section.id)"
            >
              <ChevronRight :size="14" class="trace-stage-chevron" :class="{ expanded: expandedStageIds.has(item.section.id) }" aria-hidden="true" />
              <strong>{{ traceSectionLabel(item.section, nodes) }}</strong>
              <span class="trace-stage-count">{{ item.nodes.length }} 项</span>
              <span class="trace-stage-status">{{ statusLabel(item.section.status) }}</span>
            </button>
            <ul v-if="expandedStageIds.has(item.section.id)" :id="`${viewModeId}-stage-${index}`" class="trace-stage-nodes">
              <li v-for="node in item.nodes" :key="node.id">
                <button type="button" class="trace-stage-node" :class="{ selected: node.id === selectedNodeId }" :aria-current="node.id === selectedNodeId ? 'true' : undefined" :title="node.label" @click="emit('selectNode', node)">
                  <component :is="nodeIcon(node)" :size="14" aria-hidden="true" />
                  <span class="trace-stage-node-copy"><span class="trace-stage-node-label">{{ traceNodeLabel(node) }}</span><small v-if="props.nodeSources[node.id]">{{ props.nodeSources[node.id] }}</small><small v-else-if="node.start_seq !== null">事件 #{{ node.start_seq }}</small></span>
                  <span class="trace-stage-status">{{ statusLabel(node.status) }}</span>
                </button>
              </li>
              <li v-if="!item.nodes.length" class="trace-stage-empty">暂无可展示节点</li>
            </ul>
          </template>
          <button v-else type="button" class="trace-stage-node trace-stage-standalone" :class="{ selected: item.node.id === selectedNodeId }" :aria-current="item.node.id === selectedNodeId ? 'true' : undefined" :title="item.node.label" @click="emit('selectNode', item.node)">
            <component :is="nodeIcon(item.node)" :size="14" aria-hidden="true" />
            <span class="trace-stage-node-copy"><span class="trace-stage-node-label">{{ traceNodeLabel(item.node) }}</span><small v-if="props.nodeSources[item.node.id]">{{ props.nodeSources[item.node.id] }}</small><small v-else-if="item.node.start_seq !== null">事件 #{{ item.node.start_seq }}</small></span>
            <span class="trace-stage-status">{{ statusLabel(item.node.status) }}</span>
          </button>
        </li>
      </ol>
      <template v-else>
      <details v-if="sections.length" class="trace-graph-sections">
        <summary>阶段分组<span>{{ sections.length }}</span></summary>
        <div class="trace-section-strip" aria-label="Trace 阶段">
        <button
          v-for="section in sections"
          :key="section.id"
          type="button"
          class="trace-section-chip"
          :class="{ collapsed: collapsedSectionIds.has(section.id), active: activeSectionId === section.id || (!activeSectionId && section.node_ids.includes(selectedNodeId)) }"
          :aria-expanded="!collapsedSectionIds.has(section.id)"
          :title="section.title"
          @click="toggleSection(section.id)"
        >
          <ChevronRight :size="12" class="trace-stage-chevron" :class="{ expanded: !collapsedSectionIds.has(section.id) }" aria-hidden="true" />
          <span>{{ traceSectionLabel(section, nodes) }}</span>
          <small>{{ section.node_ids.length }} 项</small>
        </button>
        </div>
      </details>
      <div
        class="trace-canvas-shell"
        :class="{ 'is-overlay': props.mode === 'overlay' }"
        @pointercancel="handlePointerUp"
        @pointerdown="handlePointerDown"
        @pointermove="handlePointerMove"
        @pointerup="handlePointerUp"
      >
        <svg
          ref="svgRef"
          class="trace-canvas trace-dag-canvas"
          :viewBox="`0 0 ${canvasWidth} ${canvasHeight}`"
          role="img"
          aria-label="当前 Run 的执行关系图"
          @wheel="handleWheel"
        >
          <defs>
            <pattern id="trace-dag-grid" width="24" height="24" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="1" fill="var(--workspace-border)" opacity=".6" />
            </pattern>
            <marker :id="markerId" markerWidth="7" markerHeight="6" refX="7" refY="3" orient="auto">
              <path d="M 0 0 L 7 3 L 0 6 Z" class="trace-edge-arrow" />
            </marker>
          </defs>
          <rect width="100%" height="100%" fill="url(#trace-dag-grid)" />
          <g :transform="`translate(${view.x} ${view.y}) scale(${view.scale})`">
            <path
              v-for="edge in layout?.edges ?? []"
              :key="edge.id"
              :d="edge.path"
              class="trace-edge"
              :class="{ active: edgeSelected(edge), dashed: edge.kind === 'continues' }"
              :marker-end="`url(#${markerId})`"
            />
            <g
              v-for="node in layout?.nodes ?? []"
              :key="node.id"
              data-trace-dag-node
              class="trace-graph-node trace-node"
              :class="[
                `tone-${nodeTone(node)}`,
                { selected: activeSectionId ? inActiveSection(node) : node.id === selectedNodeId, unresolved: node.relationship_status === 'unresolved' },
              ]"
              :transform="nodePosition(node)"
              role="button"
              tabindex="0"
              :aria-label="`${nodeText(node)}，${statusLabel(node.status)}`"
              @click="selectClickNode(node, $event)"
              @keydown.enter.prevent="selectDisplayNode(node)"
              @keydown.space.prevent="selectDisplayNode(node)"
              @pointerdown.stop="selectPointerNode(node, $event)"
            >
              <title>{{ node.section?.title ?? node.label }}</title>
              <circle v-if="nodeIsDot(node)" r="24" fill="transparent" />
              <rect v-else x="-66" y="-18" :width="PILL_WIDTH" :height="PILL_HEIGHT" rx="6" class="trace-node-pill" />
              <circle v-if="nodeIsDot(node)" :r="node.id === selectedNodeId ? 14 : DOT_RADIUS" class="trace-node-dot" />
              <circle v-if="nodeIsDot(node)" r="4" class="trace-node-dot-core" />
              <circle v-else cx="-52" r="5" class="trace-node-pill-dot" />
              <text v-if="!nodeIsDot(node)" :x="PILL_LABEL_X" y="4" class="trace-node-label">{{ nodeDisplayText(node) }}</text>
              <text v-else y="32" class="trace-node-label trace-node-label-dot">{{ nodeDisplayText(node) }}</text>
            </g>
          </g>
        </svg>
      </div>
      <div v-if="props.mode === 'embedded' && selectedNode && !activeSectionId" class="trace-selected-node">
        <div><strong :title="selectedNode.label">{{ traceNodeLabel(selectedNode) }}</strong><span>{{ statusLabel(selectedNode.status) }}</span></div>
        <p v-if="selectedNode.summary">{{ selectedNode.summary }}</p>
      </div>
      </template>
      <p v-if="dag.warnings.length" class="trace-dag-warning">部分历史关系无法验证，未连接的节点仍按原始事件保留。</p>
      <section v-if="selectedNode && showActionRecords" class="trace-actions" aria-label="节点动作记录">
        <header><strong :title="selectedNode.label">{{ traceNodeLabel(selectedNode) }}</strong><span>{{ orderedActions.length }} 条动作</span></header>
        <ol v-if="orderedActions.length">
          <li v-for="action in orderedActions" :key="action.id">
            <code>#{{ action.event_seq }}</code>
            <div><strong :title="action.label">{{ traceActionLabel(action) }}</strong><span>{{ action.summary ?? "暂无可展示的结构化结果" }}</span></div>
            <small :class="actionStatusClass(action.status)">{{ statusLabel(action.status) }}</small>
            <p
              v-if="actionReasonLabel(action)"
              class="trace-action-reason"
              :class="{ 'trace-action-reason-missing': isFailureStatus(action.status) && !action.reason }"
            >
              {{ actionReasonLabel(action) }}
            </p>
          </li>
        </ol>
        <p v-else class="trace-actions-empty">该节点没有独立的持久化动作记录。</p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.trace-dag { display: flex; min-width: 0; flex-direction: column; gap: 10px; color: var(--workspace-text); }
.trace-dag-overlay { min-height: 0; height: 100%; }
.trace-dag-toolbar { display: flex; min-height: 32px; flex: none; align-items: center; justify-content: space-between; gap: 8px; }
.trace-dag-heading { display: grid; min-width: 0; gap: 3px; }
.trace-dag-heading strong { font-size: 13px; font-weight: 600; }
.trace-dag-heading span { color: var(--workspace-text-muted); font-size: 11px; }
.trace-dag-tools { display: flex; flex: none; align-items: center; gap: 2px; }
.trace-view-mode { display: inline-flex; min-width: 0; gap: 2px; margin: 0; border: 1px solid var(--workspace-border); border-radius: 6px; background: var(--workspace-surface-subtle); padding: 3px; }
.trace-view-mode label { position: relative; display: inline-flex; height: 28px; align-items: center; justify-content: center; gap: 5px; border-radius: 4px; padding: 0 9px; color: var(--workspace-text-muted); font-size: 12px; cursor: pointer; }
.trace-view-mode input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.trace-view-mode label.active { background: var(--workspace-surface); color: var(--workspace-text); }
.trace-view-mode label:has(input:focus-visible) { outline: 2px solid var(--workspace-focus); outline-offset: 1px; }
.trace-dag-state { display: grid; min-height: 120px; place-items: center; border: 1px dashed var(--workspace-border); border-radius: var(--workspace-radius); padding: 16px; color: var(--workspace-text-muted); font-size: 12px; text-align: center; }
.trace-dag-error { color: var(--workspace-state-error); }
.trace-stage-list, .trace-stage-nodes { margin: 0; padding: 0; list-style: none; }
.trace-stage-list > li { border-bottom: 1px solid var(--workspace-border); }
.trace-stage-list > li:last-child { border-bottom: 0; }
.trace-stage-heading { display: grid; width: 100%; min-height: 42px; grid-template-columns: 14px minmax(0, 1fr) auto auto; align-items: center; gap: 8px; border: 0; background: transparent; padding: 9px 6px; color: var(--workspace-text); text-align: left; cursor: pointer; }
.trace-stage-heading strong { min-width: 0; overflow-wrap: anywhere; font-size: 13px; font-weight: 600; line-height: 1.5; }
.trace-stage-count, .trace-stage-status { color: var(--workspace-text-muted); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.trace-stage-count { color: var(--workspace-text-subtle); }
.trace-stage-chevron { flex: none; color: var(--workspace-text-subtle); transition: transform 140ms ease; }
.trace-stage-chevron.expanded { transform: rotate(90deg); }
.trace-stage-nodes { margin: 0 0 8px 12px; border-left: 1px solid var(--workspace-border); padding-left: 10px; }
.trace-stage-node { display: grid; width: 100%; min-height: 38px; grid-template-columns: 14px minmax(0, 1fr) auto; align-items: center; gap: 8px; border: 0; border-radius: 4px; background: transparent; padding: 8px 6px; color: var(--workspace-text-muted); text-align: left; cursor: pointer; }
.trace-stage-node-label { min-width: 0; overflow-wrap: anywhere; color: var(--workspace-text); font-size: 12px; line-height: 1.5; }
.trace-stage-node-copy { display: grid; min-width: 0; gap: 2px; }
.trace-stage-node-copy small { color: var(--workspace-text-subtle); font-size: 11px; }
.trace-stage-standalone { min-height: 42px; }
.trace-stage-standalone .trace-stage-node-label { font-size: 13px; }
.trace-stage-heading:hover, .trace-stage-node:hover { background: var(--workspace-surface-subtle); }
.trace-stage-node.selected { background: var(--workspace-surface-selected); box-shadow: inset 2px 0 var(--workspace-focus); }
.trace-stage-heading:focus-visible, .trace-stage-node:focus-visible { outline: 2px solid var(--workspace-focus); outline-offset: -2px; }
.trace-stage-empty { padding: 8px 6px; color: var(--workspace-text-muted); font-size: 12px; }
.trace-graph-sections { flex: none; color: var(--workspace-text-muted); font-size: 12px; }
.trace-graph-sections summary { width: fit-content; padding: 4px 0; cursor: pointer; }
.trace-graph-sections summary span { margin-left: 8px; color: var(--workspace-text-subtle); font-size: 11px; }
.trace-section-strip { display: flex; min-width: 0; flex-wrap: wrap; gap: 4px 8px; padding: 6px 0; }
.trace-section-chip { display: inline-flex; min-width: 0; align-items: center; gap: 5px; border: 0; border-radius: 4px; background: transparent; padding: 5px 6px; color: var(--workspace-text-muted); font-size: 12px; cursor: pointer; }
.trace-section-chip:hover, .trace-section-chip:focus-visible { background: var(--workspace-surface-subtle); color: var(--workspace-text); }
.trace-section-chip:focus-visible { outline: 2px solid var(--workspace-focus); }
.trace-section-chip.active { background: var(--workspace-surface-selected); color: var(--workspace-focus); box-shadow: inset 0 0 0 1px var(--workspace-border-strong); }
.trace-section-chip small { color: var(--workspace-text-subtle); font-size: 11px; }
.trace-canvas-shell { position: relative; height: 300px; min-width: 0; min-height: 0; flex: none; overflow: hidden; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface-subtle); touch-action: none; cursor: grab; }
.trace-canvas-shell:active { cursor: grabbing; }
.trace-canvas-shell.is-overlay { height: auto; min-height: 220px; flex: 1 1 0; }
.trace-canvas { display: block; width: 100%; height: 100%; }
.trace-edge { fill: none; stroke: var(--workspace-border-strong); stroke-width: 1.6; stroke-linecap: round; opacity: .7; }
.trace-edge.dashed { stroke-dasharray: 5 7; }
.trace-edge.active { stroke: var(--workspace-focus); stroke-width: 2.8; opacity: 1; stroke-dasharray: 9 7; animation: trace-edge-flow 1.3s linear infinite; }
.trace-edge-arrow { fill: var(--workspace-border-strong); }
.trace-graph-node { cursor: pointer; outline: none; }
.trace-node-pill { fill: var(--workspace-surface); stroke: var(--workspace-border-strong); stroke-width: 1.5; }
.trace-graph-node:hover .trace-node-pill, .trace-graph-node:focus-visible .trace-node-pill, .trace-graph-node.selected .trace-node-pill { stroke: var(--workspace-focus); stroke-width: 2.5; }
.trace-node-dot { fill: var(--workspace-surface); stroke: var(--workspace-border-strong); stroke-width: 2.5; }
.trace-graph-node:hover .trace-node-dot, .trace-graph-node:focus-visible .trace-node-dot, .trace-graph-node.selected .trace-node-dot { stroke: var(--workspace-focus); stroke-width: 3; }
.trace-node-dot-core, .trace-node-pill-dot { fill: var(--workspace-focus); }
.tone-tool .trace-node-dot-core { fill: var(--workspace-tool-sql); }
.tone-artifact .trace-node-dot-core { fill: var(--workspace-tool-artifact); }
.tone-terminal .trace-node-dot-core { fill: var(--workspace-state-error); }
.tone-answer .trace-node-pill-dot { fill: var(--workspace-state-success); }
.trace-graph-node.unresolved .trace-node-dot, .trace-graph-node.unresolved .trace-node-pill { stroke: var(--workspace-state-warning); stroke-dasharray: 4 3; }
.trace-node-label { fill: var(--workspace-text); font-size: 12px; font-weight: 600; }
.trace-node-label-dot { text-anchor: middle; font-size: 11px; }
.trace-selected-node { display: grid; gap: 6px; border-top: 1px solid var(--workspace-border); padding-top: 10px; }
.trace-selected-node > div { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.trace-selected-node strong { overflow-wrap: anywhere; font-size: 13px; font-weight: 600; }
.trace-selected-node span { flex: none; color: var(--workspace-text-muted); font-size: 11px; }
.trace-selected-node p { margin: 0; color: var(--workspace-text-muted); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.trace-dag-warning { margin: 0; border-left: 2px solid var(--workspace-state-warning); padding-left: 8px; color: var(--workspace-text-muted); font-size: 12px; line-height: 1.6; }
.trace-actions { display: grid; min-width: 0; gap: 8px; }
.trace-actions header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.trace-actions header strong { overflow-wrap: anywhere; font-size: 13px; font-weight: 600; }
.trace-actions header span { flex: none; color: var(--workspace-text-muted); font-size: 11px; }
.trace-actions ol { display: grid; margin: 0; padding: 0; list-style: none; }
.trace-actions li { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 6px 8px; border-top: 1px solid var(--workspace-border); padding: 9px 0; }
.trace-actions code { color: var(--workspace-text-subtle); font-size: 11px; }
.trace-actions li div { display: grid; min-width: 0; gap: 4px; }
.trace-actions li strong { overflow-wrap: anywhere; color: var(--workspace-text); font-size: 12px; font-weight: 500; }
.trace-actions li span, .trace-actions li small, .trace-actions li p, .trace-actions-empty { color: var(--workspace-text-muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.trace-actions li p { grid-column: 2 / -1; margin: 0; }
.trace-actions-empty { margin: 0; }
.trace-actions li .trace-action-success { color: var(--workspace-text-muted); }
.trace-actions li .trace-action-error { color: var(--workspace-state-error); }
.trace-actions li .trace-action-running { color: var(--workspace-state-running); }
.trace-actions li p.trace-action-reason { border-left: 2px solid var(--workspace-border-strong); padding-left: 7px; }
.trace-actions li p.trace-action-reason-missing { border-left-color: var(--workspace-state-warning); color: var(--workspace-state-warning); }
@keyframes trace-edge-flow { to { stroke-dashoffset: -32; } }
@media (prefers-reduced-motion: reduce) { .trace-edge.active { animation: none; } .trace-stage-chevron { transition: none; } }
</style>
