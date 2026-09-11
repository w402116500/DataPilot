<script setup lang="ts">
import { ChevronLeft, ChevronRight, Trash2, X } from "@lucide/vue";
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from "reka-ui";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export type CatalogOverlayItem = {
  id: string;
  title: string;
  meta: string;
  canDelete?: boolean;
};

const props = defineProps<{
  open: boolean;
  title: string;
  description: string;
  searchPlaceholder: string;
  query: string;
  items: readonly CatalogOverlayItem[];
  total: number;
  page: number;
  pageSize: number;
  loading: boolean;
  empty: string;
  currentId?: string | null;
}>();

const emit = defineEmits<{
  "update:open": [open: boolean];
  "update:query": [query: string];
  page: [page: number];
  select: [id: string];
  delete: [id: string];
}>();

const pageCount = () => Math.max(1, Math.ceil(props.total / props.pageSize) || 1);
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="catalog-overlay">
        <div class="catalog-overlay-backdrop" aria-hidden="true" />
        <DialogContent as="section" class="catalog-overlay-panel" :aria-label="title">
          <header class="catalog-overlay-header">
            <div class="catalog-overlay-title">
              <DialogTitle class="catalog-overlay-title-line">{{ title }}</DialogTitle>
              <DialogDescription>{{ description }}</DialogDescription>
            </div>
            <DialogClose as-child>
              <Button variant="ghost" size="icon-sm" :title="`关闭${title}`">
                <X :size="17" aria-hidden="true" /><span class="sr-only">关闭</span>
              </Button>
            </DialogClose>
          </header>
          <div class="catalog-overlay-toolbar">
            <Input
              :model-value="query"
              :placeholder="searchPlaceholder"
              @update:model-value="emit('update:query', String($event))"
            />
          </div>
          <div class="catalog-overlay-body">
            <p v-if="loading" class="catalog-empty">正在加载…</p>
            <p v-else-if="items.length === 0" class="catalog-empty">{{ empty }}</p>
            <div v-else class="catalog-list">
              <div
                v-for="item in items"
                :key="item.id"
                class="catalog-row"
                :class="{ active: item.id === currentId }"
              >
                <button type="button" class="catalog-row-main" @click="emit('select', item.id)">
                  <span>{{ item.title }}</span>
                  <small>{{ item.meta }}</small>
                </button>
                <Button
                  v-if="item.canDelete"
                  variant="ghost"
                  size="icon-sm"
                  title="删除会话"
                  @click="emit('delete', item.id)"
                >
                  <Trash2 :size="14" aria-hidden="true" /><span class="sr-only">删除会话</span>
                </Button>
              </div>
            </div>
          </div>
          <footer class="catalog-overlay-footer">
            <span>第 {{ page }} / {{ pageCount() }} 页 · 共 {{ total }} 条</span>
            <div class="catalog-pager">
              <Button
                variant="outline"
                size="sm"
                :disabled="page <= 1 || loading"
                @click="emit('page', page - 1)"
              >
                <ChevronLeft :size="14" aria-hidden="true" />上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                :disabled="page >= pageCount() || loading"
                @click="emit('page', page + 1)"
              >
                下一页<ChevronRight :size="14" aria-hidden="true" />
              </Button>
            </div>
          </footer>
        </DialogContent>
      </DialogOverlay>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.catalog-overlay { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 12px; }
.catalog-overlay-backdrop { position: absolute; inset: 0; background: var(--workspace-overlay-scrim); }
.catalog-overlay-panel { position: relative; display: flex; width: min(720px, 100%); height: min(84dvh, 720px); min-height: 0; flex-direction: column; overflow: hidden; border: 1px solid var(--workspace-border-strong); border-radius: var(--workspace-radius); background: var(--workspace-surface); box-shadow: var(--workspace-shadow-overlay); outline: none; }
.catalog-overlay-header { display: flex; flex: none; align-items: flex-start; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--workspace-border); padding: 15px 18px 13px; }
.catalog-overlay-title { display: grid; min-width: 0; gap: 4px; }
.catalog-overlay-title-line { margin: 0; color: var(--workspace-text); font-size: 17px; }
.catalog-overlay-title :deep(p) { margin: 0; color: var(--workspace-text-muted); font-size: 12px; }
.catalog-overlay-toolbar { flex: none; padding: 12px 18px 0; }
.catalog-overlay-body { display: grid; min-height: 0; flex: 1 1 0; overflow: auto; padding: 12px 14px; }
.catalog-list { display: grid; align-content: start; gap: 4px; }
.catalog-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; border-radius: var(--workspace-radius-sm); }
.catalog-row.active { background: var(--workspace-surface-selected); }
.catalog-row-main { display: grid; min-width: 0; gap: 3px; padding: 10px 8px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.catalog-row-main span { overflow: hidden; color: var(--workspace-text); text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.catalog-row-main small { color: var(--workspace-text-muted); font-size: 11px; }
.catalog-row:hover { background: var(--workspace-surface-hover); }
.catalog-empty { margin: 24px 8px; color: var(--workspace-text-muted); font-size: 13px; }
.catalog-overlay-footer { display: flex; flex: none; align-items: center; justify-content: space-between; gap: 12px; border-top: 1px solid var(--workspace-border); padding: 10px 14px 12px; color: var(--workspace-text-muted); font-size: 12px; }
.catalog-pager { display: flex; gap: 8px; }
@media (max-width: 860px) {
  .catalog-overlay { padding: 0; }
  .catalog-overlay-panel { width: 100%; height: 100dvh; border-radius: 0; }
}
</style>
