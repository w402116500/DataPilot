<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Download,
  FileDown,
  Image,
  Search,
} from "@lucide/vue";

import type { BinaryResponse } from "@/api/client";
import type { RunArtifact, TableData } from "@/api/types";
import RichMarkdown from "@/components/RichMarkdown.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  artifactReaderKind,
  filterArtifactRows,
  formatArtifactCell,
  formatJsonArtifact,
  isSafeChartMime,
  normalizeArtifactMime,
  parseDelimitedArtifact,
  parseTableArtifact,
  sortArtifactRows,
  type ArtifactTableSort,
} from "@/lib/artifactContent";
import { useArtifactStore } from "@/stores/artifactStore";

const props = withDefaults(defineProps<{
  artifact: RunArtifact | null;
  maxRows?: number;
  hideDownload?: boolean;
  compact?: boolean;
}>(), {
  maxRows: 50,
  hideDownload: false,
  compact: false,
});
const artifactStore = useArtifactStore();

const tableData = ref<TableData | null>(null);
const markdownContent = ref("");
const codeContent = ref<string | null>(null);
const imageUrl = ref<string | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const page = ref(1);
const searchQuery = ref("");
const sort = ref<ArtifactTableSort | null>(null);
let loadToken = 0;
let contentAbortController: AbortController | null = null;

const readerKind = computed(() => props.artifact === null ? "download" : artifactReaderKind(props.artifact));
const pageSize = computed(() => Math.max(1, props.maxRows));
const processedRows = computed(() =>
  sortArtifactRows(filterArtifactRows(tableData.value?.rows ?? [], searchQuery.value), sort.value),
);
const tablePageCount = computed(() =>
  Math.max(1, Math.ceil(processedRows.value.length / pageSize.value)),
);
const tableRows = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return processedRows.value.slice(start, start + pageSize.value);
});
const visibleColumns = computed(() => {
  const columns = tableData.value?.columns ?? [];
  return props.compact ? columns.slice(0, 4) : columns;
});
const tableSummary = computed(() => {
  const data = tableData.value;
  if (data === null) return "";
  if (props.compact) return `展示 ${tableRows.value.length} 行预览，共 ${data.row_count} 行`;
  if (searchQuery.value.trim()) {
    return `匹配 ${processedRows.value.length} / 已加载 ${data.rows.length} 行`;
  }
  if (data.row_count !== data.rows.length) return `已加载 ${data.rows.length} / 共 ${data.row_count} 行`;
  return `共 ${data.row_count} 行`;
});

function clearContent(): void {
  tableData.value = null;
  markdownContent.value = "";
  codeContent.value = null;
  page.value = 1;
  searchQuery.value = "";
  sort.value = null;
  if (imageUrl.value !== null) URL.revokeObjectURL(imageUrl.value);
  imageUrl.value = null;
}

function cancelContentLoad(): void {
  contentAbortController?.abort();
  contentAbortController = null;
}

function errorMessage(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message ? caught.message : fallback;
}

function isAbortError(caught: unknown): boolean {
  return caught instanceof Error && caught.name === "AbortError";
}

function assertResponseMime(response: BinaryResponse, artifact: RunArtifact): void {
  if (normalizeArtifactMime(response.contentType) !== normalizeArtifactMime(artifact.mime_type)) {
    throw new Error("产物响应格式与登记信息不一致");
  }
}

async function loadContent(): Promise<void> {
  const artifact = props.artifact;
  const kind = artifact === null ? "download" : artifactReaderKind(artifact);
  const token = ++loadToken;
  cancelContentLoad();
  clearContent();
  error.value = null;
  if (artifact === null || kind === "download") {
    loading.value = false;
    return;
  }

  const controller = new AbortController();
  contentAbortController = controller;
  loading.value = true;
  try {
    const response = artifactStore.contentById[artifact.id]
      ?? await artifactStore.readContent(artifact.id, controller.signal);
    if (token !== loadToken) return;
    assertResponseMime(response, artifact);

    if (kind === "image") {
      if (!isSafeChartMime(response.contentType)) throw new Error("图表产物不是安全图片");
      imageUrl.value = URL.createObjectURL(response.blob);
      return;
    }

    const text = await response.blob.text();
    if (token !== loadToken) return;
    if (kind === "table") {
      tableData.value = parseTableArtifact(text);
      if (tableData.value === null) throw new Error("表格产物格式无效");
    } else if (kind === "csv" || kind === "tsv") {
      tableData.value = parseDelimitedArtifact(text, kind === "tsv" ? "\t" : ",");
      if (tableData.value === null) throw new Error(`${kind.toUpperCase()} 文件格式无效`);
    } else if (kind === "markdown") {
      markdownContent.value = text;
    } else if (kind === "json") {
      codeContent.value = formatJsonArtifact(text);
      if (codeContent.value === null) throw new Error("JSON 文件格式无效");
    } else if (kind === "text") {
      codeContent.value = text;
    }
  } catch (caught) {
    if (token === loadToken && !isAbortError(caught)) {
      error.value = errorMessage(caught, "读取产物失败");
    }
  } finally {
    if (token === loadToken) {
      loading.value = false;
      if (contentAbortController === controller) contentAbortController = null;
    }
  }
}

function toggleSort(columnIndex: number): void {
  const current = sort.value;
  if (current === null || current.columnIndex !== columnIndex) {
    sort.value = { columnIndex, direction: "asc" };
  } else if (current.direction === "asc") {
    sort.value = { columnIndex, direction: "desc" };
  } else {
    sort.value = null;
  }
  page.value = 1;
}

function sortLabel(column: string, columnIndex: number): string {
  const current = sort.value;
  if (current?.columnIndex !== columnIndex) return `按 ${column} 升序排列`;
  return current.direction === "asc" ? `按 ${column} 降序排列` : `取消 ${column} 排序`;
}

async function download(): Promise<void> {
  const artifact = props.artifact;
  if (artifact === null) return;
  loading.value = true;
  error.value = null;
  try {
    const response = await artifactStore.download(artifact.id);
    const objectUrl = URL.createObjectURL(response.blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = response.filename ?? "artifact";
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  } catch (caught) {
    error.value = errorMessage(caught, "下载产物失败");
  } finally {
    loading.value = false;
  }
}

watch(
  () => [
    props.artifact?.id,
    props.artifact?.mime_type,
    props.artifact?.size_bytes,
    props.artifact?.inline_previewable,
  ],
  () => { void loadContent(); },
  { immediate: true },
);
watch(searchQuery, () => { page.value = 1; });
watch(tablePageCount, (count) => { page.value = Math.min(page.value, count); });
onBeforeUnmount(() => {
  loadToken += 1;
  cancelContentLoad();
  clearContent();
});
</script>

<template>
  <section class="artifact-viewer" :class="{ 'is-compact': compact }" aria-label="产物查看器">
    <p v-if="artifact === null" class="artifact-empty">选择一个产物查看详情。</p>
    <template v-else>
      <header v-if="!compact" class="artifact-header">
        <div>
          <strong>{{ artifact.title }}</strong>
          <span>{{ artifact.mime_type }} · {{ artifact.size_bytes.toLocaleString() }} B</span>
        </div>
        <Button v-if="!hideDownload" variant="outline" size="sm" :disabled="loading" @click="download">
          <Download :size="14" aria-hidden="true" />下载
        </Button>
      </header>

      <p v-if="error" class="artifact-error">{{ error }}</p>
      <p v-else-if="loading" class="artifact-empty">正在读取受控产物...</p>

      <div v-else-if="readerKind === 'download'" class="file-artifact">
        <FileDown :size="20" aria-hidden="true" />
        <p>{{ artifact.inline_previewable ? "当前格式无法在线阅读。" : "此产物仅支持下载。" }}</p>
      </div>

      <template v-else-if="readerKind === 'table' || readerKind === 'csv' || readerKind === 'tsv'">
        <p v-if="tableData === null" class="artifact-empty">没有可显示的表格内容。</p>
        <template v-else>
          <div v-if="!compact" class="table-toolbar">
            <label class="table-search">
              <Search :size="14" aria-hidden="true" />
              <span class="sr-only">搜索表格</span>
              <Input v-model="searchQuery" type="search" placeholder="搜索当前结果" />
            </label>
            <span>{{ tableSummary }}</span>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th v-for="(column, columnIndex) in visibleColumns" :key="`${column}-${columnIndex}`">
                    <button
                      v-if="!compact"
                      type="button"
                      class="table-sort"
                      :aria-label="sortLabel(column, columnIndex)"
                      @click="toggleSort(columnIndex)"
                    >
                      <span>{{ column }}</span>
                      <ArrowUp v-if="sort?.columnIndex === columnIndex && sort.direction === 'asc'" :size="12" aria-hidden="true" />
                      <ArrowDown v-else-if="sort?.columnIndex === columnIndex" :size="12" aria-hidden="true" />
                      <ArrowUpDown v-else :size="12" aria-hidden="true" />
                    </button>
                    <span v-else>{{ column }}</span>
                  </th>
                  <th v-if="compact && tableData.columns.length > visibleColumns.length" class="more-column">...</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, rowIndex) in tableRows" :key="`${page}-${rowIndex}`">
                  <td
                    v-for="(cell, cellIndex) in row.slice(0, visibleColumns.length)"
                    :key="cellIndex"
                    :title="formatArtifactCell(cell)"
                  >
                    {{ formatArtifactCell(cell) }}
                  </td>
                  <td v-if="compact && tableData.columns.length > visibleColumns.length" class="more-column">...</td>
                </tr>
                <tr v-if="tableRows.length === 0">
                  <td :colspan="visibleColumns.length || 1" class="empty-cell">没有匹配的行</td>
                </tr>
              </tbody>
            </table>
          </div>
          <footer class="table-pagination">
            <span>{{ tableSummary }}<template v-if="!compact"> · 第 {{ page }}/{{ tablePageCount }} 页</template></span>
            <div v-if="!compact">
              <Button variant="outline" size="sm" :disabled="page <= 1" @click="page -= 1">上一页</Button>
              <Button variant="outline" size="sm" :disabled="page >= tablePageCount" @click="page += 1">下一页</Button>
            </div>
          </footer>
        </template>
      </template>

      <div v-else-if="readerKind === 'image' && imageUrl" class="image-artifact">
        <Image :size="16" aria-hidden="true" />
        <img :src="imageUrl" :alt="artifact.title">
      </div>
      <p v-else-if="readerKind === 'image'" class="artifact-empty">没有可显示的图片。</p>
      <RichMarkdown v-else-if="readerKind === 'markdown'" class="markdown-artifact" :content="markdownContent" density="artifact" />
      <pre v-else-if="readerKind === 'json' || readerKind === 'text'" class="code-artifact"><code>{{ codeContent }}</code></pre>
    </template>
  </section>
</template>

<style scoped>
.artifact-viewer { display: grid; min-width: 0; gap: 10px; border-top: 1px solid var(--workspace-border); padding-top: 10px; }
.artifact-viewer.is-compact { gap: 7px; border-top: 0; padding-top: 0; }
.artifact-header, .artifact-header > div, .table-toolbar, .table-search, .table-pagination, .table-pagination > div, .file-artifact, .image-artifact { display: flex; align-items: center; }
.artifact-header { justify-content: space-between; gap: 8px; }
.artifact-header > div { min-width: 0; flex-direction: column; align-items: flex-start; gap: 2px; }
.artifact-header strong { overflow: hidden; max-width: 100%; color: var(--workspace-text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.artifact-header span, .artifact-empty { color: var(--workspace-text-muted); font-size: 11px; line-height: 1.5; }
.artifact-empty, .artifact-error { margin: 0; }
.artifact-error { border-left: 2px solid var(--workspace-state-error); padding-left: 7px; color: var(--workspace-state-error); font-size: 11px; line-height: 1.5; }
.table-toolbar { justify-content: space-between; gap: 8px; color: var(--workspace-text-muted); font-size: 10px; }
.table-search { position: relative; min-width: 160px; flex: 1; }
.table-search > svg { position: absolute; left: 9px; z-index: 1; color: var(--workspace-text-muted); pointer-events: none; }
.table-search :deep(input) { height: 32px; padding-left: 29px; border-radius: var(--workspace-radius-sm); font-size: 11px; }
.table-scroll { max-width: 100%; overflow: auto; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); overscroll-behavior: contain; }
.is-compact .table-scroll { max-height: 150px; }
.table-scroll table { width: 100%; min-width: max-content; border-collapse: collapse; font-size: 11px; }
.table-scroll th, .table-scroll td { max-width: 220px; border-bottom: 1px solid var(--workspace-border); padding: 6px 8px; text-align: left; }
.table-scroll th { position: sticky; top: 0; z-index: 1; background: var(--workspace-table-header); color: var(--workspace-text); font-weight: 700; }
.table-scroll td { overflow: hidden; color: var(--workspace-text); text-overflow: ellipsis; white-space: nowrap; }
.table-scroll tbody tr:hover { background: var(--workspace-table-row-hover); }
.table-sort { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 8px; border: 0; background: transparent; padding: 0; color: inherit; font: inherit; cursor: pointer; }
.table-sort span { overflow: hidden; text-overflow: ellipsis; }
.more-column { width: 28px; color: var(--workspace-text-muted) !important; text-align: center !important; }
.empty-cell { color: var(--workspace-text-muted) !important; text-align: center !important; }
.table-pagination { min-height: 28px; justify-content: space-between; gap: 8px; color: var(--workspace-text-muted); font-size: 10px; }
.table-pagination > div { gap: 5px; }
.file-artifact { flex-direction: column; justify-content: center; gap: 8px; border: 1px dashed var(--workspace-border-strong); border-radius: var(--workspace-radius-sm); padding: 18px 10px; color: var(--workspace-text-muted); text-align: center; }
.is-compact .file-artifact { min-height: 66px; padding: 10px; }
.file-artifact p { margin: 0; font-size: 11px; line-height: 1.5; }
.image-artifact { flex-direction: column; gap: 7px; min-width: 0; max-width: 100%; color: var(--workspace-tool-artifact); }
.image-artifact img { width: auto; max-width: 100%; max-height: min(640px, 65vh); height: auto; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); object-fit: contain; }
.markdown-artifact { min-width: 0; max-width: 100%; }
.is-compact .image-artifact img { max-height: 140px; }
.is-compact .markdown-artifact { max-height: 140px; overflow: hidden; pointer-events: none; font-size: 11px; }
.code-artifact { max-height: min(640px, 65vh); margin: 0; overflow: auto; border: 1px solid var(--workspace-code-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-code-background); padding: 10px; color: var(--workspace-code-text); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.is-compact .code-artifact { max-height: 120px; padding: 8px; font-size: 10px; }
@media (max-width: 760px) { .image-artifact img { max-height: min(240px, 40vh); } }
@media (max-width: 420px) { .artifact-header { align-items: flex-start; }.table-toolbar, .table-pagination { flex-wrap: wrap; }.table-search { min-width: 100%; }.table-pagination > div { width: 100%; justify-content: flex-end; } }
</style>
