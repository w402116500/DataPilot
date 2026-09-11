<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";

import MarkdownRender from "markstream-vue";
import { presentAssistantAnswer } from "@/lib/answerPresentation";
import { useTheme } from "@/lib/theme";

const props = withDefaults(defineProps<{
  content: string;
  density?: "chat" | "artifact";
  answer?: boolean;
  streaming?: boolean;
  anchorPrefix?: string;
}>(), {
  density: "chat",
  answer: false,
  streaming: false,
  anchorPrefix: "",
});

const NUMERIC_CELL = /^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$/u;

const { isDark } = useTheme();
const markdownRoot = ref<HTMLElement | null>(null);
const content = computed(() => props.answer
  ? presentAssistantAnswer(props.content)
  : props.content);
const mode = computed<"chat" | "docs">(() => props.density === "artifact" ? "docs" : "chat");

function isNumericCell(text: string): boolean {
  const value = text.replace(/\s+/gu, "");
  return value === "" || NUMERIC_CELL.test(value);
}

function applyNumericColumns(root: HTMLElement): void {
  root.querySelectorAll("table").forEach((table) => {
    table.querySelectorAll(".rich-md-numeric").forEach((cell) => {
      cell.classList.remove("rich-md-numeric");
    });
    const bodyRows = [...table.querySelectorAll("tbody tr")];
    if (bodyRows.length === 0) return;
    const colCount = Math.max(0, ...bodyRows.map((row) => row.children.length));
    for (let index = 0; index < colCount; index += 1) {
      const cells = bodyRows
        .map((row) => row.children.item(index))
        .filter((cell): cell is HTMLElement => cell instanceof HTMLElement);
      if (cells.length === 0) continue;
      const values = cells.map((cell) => (cell.textContent ?? "").trim());
      if (values.every((value) => value === "")) continue;
      if (!values.every((value) => isNumericCell(value))) continue;
      for (const cell of cells) cell.classList.add("rich-md-numeric");
      table.querySelectorAll("thead tr").forEach((row) => {
        const header = row.children.item(index);
        if (header instanceof HTMLElement) header.classList.add("rich-md-numeric");
      });
    }
  });
}

async function applyPresentation(): Promise<void> {
  await nextTick();
  const root = markdownRoot.value;
  if (root === null) return;
  if (props.anchorPrefix) {
    root.querySelectorAll<HTMLElement>("h1, h2, h3, h4").forEach((heading, index) => {
      heading.id = `${props.anchorPrefix}-heading-${index + 1}`;
      heading.tabIndex = -1;
      heading.classList.add("answer-heading-anchor");
    });
  }
  applyNumericColumns(root);
}

watch(content, applyPresentation);
onMounted(applyPresentation);
</script>

<template>
  <article ref="markdownRoot" class="rich-markdown" :class="`rich-markdown-${density}`">
    <MarkdownRender
      :content="content"
      :mode="mode"
      :final="!props.streaming"
      :fade="false"
      :is-dark="isDark"
      :render-code-blocks-as-pre="true"
      html-policy="escape"
    />
  </article>
</template>

<style scoped>
.rich-markdown {
  min-width: 0;
  max-width: 100%;
  color: var(--workspace-text);
  overflow-wrap: anywhere;
  word-break: normal;
}
.rich-markdown :deep(.markstream-vue) {
  --ms-radius: var(--workspace-radius);
  --ms-font-sans: inherit;
  --ms-font-mono: ui-monospace, SFMono-Regular, Consolas, monospace;
  --ms-size-image-max-width: 100%;
  --ms-size-image-min-width: 0;
  --ms-flow-paragraph-y: .55em;
  --ms-flow-list-y: .5em;
  --ms-flow-list-item-y: .18em;
  --ms-flow-list-indent: 1.35em;
  --ms-flow-table-y: .85em;
  --ms-flow-table-cell: .55em .85em;
  --ms-flow-blockquote-y: .75em;
  --ms-flow-hr-y: 1.15em;
  --ms-flow-codeblock-y: .75em;
  --ms-flow-heading-1-mt: 0;
  --ms-flow-heading-1-mb: .55em;
  --ms-flow-heading-2-mt: 1.2em;
  --ms-flow-heading-2-mb: .5em;
  --ms-flow-heading-3-mt: 1em;
  --ms-flow-heading-3-mb: .4em;
  --ms-flow-heading-4-mt: .85em;
  --ms-flow-heading-4-mb: .35em;
  --link-color: var(--workspace-focus);
  --list-marker: var(--workspace-text-muted);
  --list-counter-marker: var(--workspace-text-muted);
  --hr-border: var(--workspace-border);
  --blockquote-border: var(--workspace-focus);
  --table-border: var(--workspace-border);
  --table-header-bg: var(--workspace-table-header);
  --inline-code-bg: var(--workspace-surface-inset);
  --inline-code-fg: var(--workspace-code-text);
  --inline-code-border: var(--workspace-code-border);
  --code-bg: var(--workspace-code-background);
  --code-fg: var(--workspace-code-text);
  --code-border: var(--workspace-code-border);
  max-width: 100%;
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--workspace-text);
}
.rich-markdown-chat :deep(.markstream-vue) {
  --ms-text-body: 14px;
  --ms-leading-body: 1.7;
  --ms-text-h1: 18px;
  --ms-text-h2: 16px;
  --ms-text-h3: 14px;
  --ms-text-h4: 13px;
  --ms-weight-h1: 700;
  --ms-weight-h2: 650;
  --ms-weight-h3: 650;
}
.rich-markdown-artifact :deep(.markstream-vue) {
  --ms-text-body: 12px;
  --ms-leading-body: 1.6;
  --ms-text-h1: 16px;
  --ms-text-h2: 14px;
  --ms-text-h3: 13px;
  --ms-text-h4: 12px;
  --ms-flow-heading-2-mt: 1em;
  --ms-flow-table-cell: .45em .7em;
}
.rich-markdown :deep(.markdown-renderer),
.rich-markdown :deep(.node-slot),
.rich-markdown :deep(.node-content),
.rich-markdown :deep(.text-node) {
  min-width: 0;
  max-width: 100%;
}
.rich-markdown :deep(.markdown-renderer) {
  content-visibility: visible;
  contain-intrinsic-size: none;
}
.rich-markdown :deep(.text-node) {
  overflow-wrap: anywhere;
}
.rich-markdown :deep(.markstream-vue h1),
.rich-markdown :deep(.markstream-vue h2),
.rich-markdown :deep(.markstream-vue h3),
.rich-markdown :deep(.markstream-vue h4),
.rich-markdown :deep(.markstream-vue h5),
.rich-markdown :deep(.markstream-vue h6) {
  color: var(--workspace-text);
  letter-spacing: 0;
}
.rich-markdown :deep(.markstream-vue .node-slot:first-child .heading-node),
.rich-markdown :deep(.markstream-vue > .heading-node:first-child) {
  margin-top: 0;
}
.rich-markdown :deep(.markstream-vue h2) {
  padding-bottom: 6px;
  border-bottom: 1px solid var(--workspace-border);
}
.rich-markdown :deep(.markstream-vue p) {
  color: var(--workspace-text);
}
.rich-markdown :deep(.markstream-vue strong) {
  color: var(--workspace-text);
  font-weight: 650;
}
.rich-markdown :deep(.markstream-vue em) {
  color: var(--workspace-text);
}
.rich-markdown :deep(.markstream-vue a) {
  color: var(--workspace-focus);
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, var(--workspace-focus) 38%, transparent);
  text-underline-offset: 3px;
}
.rich-markdown :deep(.markstream-vue a:hover) {
  text-decoration-color: currentColor;
}
.rich-markdown :deep(.markstream-vue ul),
.rich-markdown :deep(.markstream-vue ol) {
  color: var(--workspace-text);
}
.rich-markdown :deep(.markstream-vue li::marker) {
  color: var(--workspace-text-muted);
}
.rich-markdown :deep(.markstream-vue blockquote) {
  border-left: 3px solid var(--workspace-focus);
  background: var(--workspace-surface-subtle);
  padding: 8px 12px;
  color: var(--workspace-text);
  border-radius: 0 var(--workspace-radius-sm) var(--workspace-radius-sm) 0;
}
.rich-markdown :deep(.markstream-vue blockquote p) {
  color: inherit;
}
.rich-markdown :deep(.markstream-vue hr) {
  border: 0;
  border-top: 1px solid var(--workspace-border);
}
.rich-markdown :deep(.table-node-wrapper) {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow-x: auto;
  border-radius: var(--workspace-radius);
  background: transparent;
  -webkit-overflow-scrolling: touch;
}
.rich-markdown :deep(.table-node__resize-handle) {
  display: none;
}
.rich-markdown :deep(.table-node),
.rich-markdown :deep(.markstream-vue table) {
  display: table;
  width: max-content;
  min-width: min(20rem, 100%);
  max-width: none;
  table-layout: auto;
  margin: 0;
  overflow: hidden;
  border: 1px solid var(--workspace-border);
  border-radius: var(--workspace-radius);
  background: var(--workspace-surface-subtle);
  box-shadow: none;
}
.rich-markdown :deep(.markstream-vue table thead),
.rich-markdown :deep(.markstream-vue table tbody),
.rich-markdown :deep(.markstream-vue table tfoot) {
  display: revert;
}
.rich-markdown :deep(.markstream-vue th),
.rich-markdown :deep(.markstream-vue td) {
  border-bottom: 1px solid var(--workspace-border);
  border-right: 1px solid var(--workspace-border);
  padding: var(--ms-flow-table-cell);
  overflow-wrap: anywhere;
  white-space: normal;
  text-align: left;
  vertical-align: top;
}
.rich-markdown :deep(.markstream-vue th) {
  background: var(--workspace-table-header);
  color: var(--workspace-text-muted);
  font-size: 11px;
  font-weight: 650;
  letter-spacing: .02em;
}
.rich-markdown :deep(.markstream-vue td) {
  color: var(--workspace-text);
  font-size: 13px;
}
.rich-markdown :deep(.markstream-vue tbody tr:nth-child(2n)) {
  background: transparent;
}
.rich-markdown :deep(.markstream-vue tbody tr:hover) {
  background: var(--workspace-table-row-hover);
}
.rich-markdown :deep(.markstream-vue tr:last-child td) {
  border-bottom: 0;
}
.rich-markdown :deep(.markstream-vue th[align="right"]),
.rich-markdown :deep(.markstream-vue td[align="right"]),
.rich-markdown :deep(.markstream-vue .text-right),
.rich-markdown :deep(.markstream-vue .rich-md-numeric),
.rich-markdown :deep(.markstream-vue .text-left.rich-md-numeric) {
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  white-space: nowrap;
}
.rich-markdown :deep(.markstream-vue pre),
.rich-markdown :deep(.markstream-vue img),
.rich-markdown :deep(.image-node-container),
.rich-markdown :deep(.image-placeholder),
.rich-markdown :deep(.image-error) {
  max-width: 100%;
}
.rich-markdown :deep(.image-node-container),
.rich-markdown :deep(.image-placeholder),
.rich-markdown :deep(.image-error) {
  display: block;
  min-width: 0;
}
.rich-markdown :deep(.markstream-vue code),
.rich-markdown :deep(.inline-code) {
  border: 1px solid var(--workspace-code-border);
  border-radius: var(--workspace-radius-sm);
  background: var(--workspace-surface-inset);
  padding: .08em .4em;
  color: var(--workspace-code-text);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: .86em;
  overflow-wrap: anywhere;
}
.rich-markdown :deep(.markstream-vue pre) {
  overflow: auto;
  border: 1px solid var(--workspace-code-border);
  border-radius: var(--workspace-radius);
  background: var(--workspace-code-background);
  padding: 12px 14px;
  color: var(--workspace-code-text);
  line-height: 1.6;
}
.rich-markdown :deep(.markstream-vue pre code) {
  border: 0;
  background: transparent;
  padding: 0;
  color: inherit;
  font-size: inherit;
}
.rich-markdown :deep(.markstream-vue img),
.rich-markdown :deep(.image-node__img) {
  display: block;
  width: auto;
  max-width: 100%;
  height: auto;
  min-width: 0;
  border: 1px solid var(--workspace-border);
  border-radius: var(--workspace-radius);
  object-fit: contain;
}
.rich-markdown :deep(.answer-heading-anchor) {
  scroll-margin-top: 20px;
}
@media (max-width: 760px) {
  .rich-markdown-chat :deep(.markstream-vue) {
    --ms-text-body: 13px;
    --ms-leading-body: 1.65;
    --ms-text-h1: 17px;
    --ms-text-h2: 15px;
  }
  .rich-markdown :deep(.markstream-vue th),
  .rich-markdown :deep(.markstream-vue td) {
    padding: 7px 9px;
  }
  .rich-markdown :deep(.markstream-vue img),
  .rich-markdown :deep(.image-node__img) {
    max-height: min(240px, 45vh);
  }
}
</style>
