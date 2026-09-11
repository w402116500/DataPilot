<script setup lang="ts">
import { Network, X } from "@lucide/vue";
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";

import { Button } from "@/components/ui/button";

defineProps<{
  open: boolean;
  rootName: string | null;
  graphVersion: string | null;
}>();

const emit = defineEmits<{
  "update:open": [open: boolean];
}>();
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="graph-overlay">
        <div class="graph-overlay-backdrop" aria-hidden="true" />
        <DialogContent as="section" class="graph-overlay-panel" aria-label="语义地图">
          <header class="graph-overlay-header">
            <div class="graph-overlay-title">
              <DialogTitle class="graph-overlay-title-line"><Network :size="16" aria-hidden="true" />语义地图</DialogTitle>
              <p v-if="rootName" class="graph-overlay-kicker">{{ rootName }}<template v-if="graphVersion"> · {{ graphVersion }}</template></p>
              <DialogDescription class="sr-only">当前数据源语义地图的全屏视图，使用同一份已加载的实体、属性和语义关系。</DialogDescription>
            </div>
            <DialogClose as-child>
              <Button variant="ghost" size="icon-sm" title="关闭语义地图">
                <X :size="17" aria-hidden="true" /><span class="sr-only">关闭语义地图</span>
              </Button>
            </DialogClose>
          </header>
          <div class="graph-overlay-body">
            <slot />
          </div>
        </DialogContent>
      </DialogOverlay>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.graph-overlay { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 12px; }
.graph-overlay-backdrop { position: absolute; inset: 0; background: var(--workspace-overlay-scrim); }
.graph-overlay-panel { position: relative; display: flex; width: min(1500px, 100%); height: min(96dvh, 980px); min-height: 0; flex-direction: column; overflow: hidden; border: 1px solid var(--workspace-border-strong); border-radius: var(--workspace-radius); background: var(--workspace-surface); box-shadow: var(--workspace-shadow-overlay); outline: none; }
.graph-overlay-header { display: flex; flex: none; align-items: flex-start; justify-content: space-between; gap: 20px; border-bottom: 1px solid var(--workspace-border); padding: 15px 18px 13px; }
.graph-overlay-title { display: grid; min-width: 0; gap: 3px; }
.graph-overlay-title-line { display: flex; align-items: center; gap: 8px; margin: 0; color: var(--workspace-text); font-size: 17px; }
.graph-overlay-title-line svg { color: var(--workspace-text-muted); }
.graph-overlay-kicker { margin: 0; overflow: hidden; color: var(--workspace-text-muted); text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
.graph-overlay-body { display: grid; min-height: 0; flex: 1 1 0; padding: 12px 14px 14px; }
.graph-overlay-body :deep(.graph-panel) { height: 100%; min-height: 0; }
@media (max-width: 860px) {
  .graph-overlay { padding: 0; }
  .graph-overlay-panel { width: 100%; height: 100dvh; border-radius: 0; }
  .graph-overlay-header { gap: 12px; padding: 13px 14px 11px; }
  .graph-overlay-body { overflow: hidden; padding: 8px; }
  .graph-overlay-body :deep(.graph-panel) { height: 100%; min-height: 0; }
}
</style>
