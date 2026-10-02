<script setup lang="ts">
import type { DialogContentEmits, DialogContentProps } from "reka-ui";
import type { HTMLAttributes } from "vue";
import { reactiveOmit } from "@vueuse/core";
import { DialogContent, DialogOverlay, DialogPortal, useForwardPropsEmits } from "reka-ui";

import { cn } from "@/lib/utils";

const props = defineProps<DialogContentProps & { class?: HTMLAttributes["class"] }>();
const emit = defineEmits<DialogContentEmits>();
const delegatedProps = reactiveOmit(props, "class");
const forwarded = useForwardPropsEmits(delegatedProps, emit);
</script>

<template>
  <DialogPortal>
    <DialogOverlay class="fixed inset-0 z-50 bg-workspace-overlay-scrim backdrop-blur-sm" />
    <DialogContent v-bind="forwarded" :class="cn('fixed left-1/2 top-1/2 z-50 grid max-h-[calc(100dvh-2rem)] w-[min(92vw,32rem)] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto overscroll-contain rounded-xl border border-workspace-border-strong bg-card/95 p-5 text-card-foreground shadow-2xl backdrop-blur-2xl focus:outline-none', props.class)">
      <slot />
    </DialogContent>
  </DialogPortal>
</template>
