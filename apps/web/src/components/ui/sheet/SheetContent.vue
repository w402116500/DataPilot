<script setup lang="ts">
import type { DialogContentEmits, DialogContentProps } from "reka-ui";
import type { HTMLAttributes } from "vue";
import { reactiveOmit } from "@vueuse/core";
import {
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  useForwardPropsEmits,
} from "reka-ui";

import { cn } from "@/lib/utils";

const props = withDefaults(
  defineProps<DialogContentProps & { class?: HTMLAttributes["class"]; side?: "left" | "right" }>(),
  { side: "right" },
);
const emit = defineEmits<DialogContentEmits>();
const delegatedProps = reactiveOmit(props, "class", "side");
const forwarded = useForwardPropsEmits(delegatedProps, emit);
</script>

<template>
  <DialogPortal>
    <DialogOverlay class="fixed inset-0 z-40 bg-black/35" />
    <DialogContent
      v-bind="forwarded"
      :class="cn('fixed inset-y-0 z-50 flex w-[min(88vw,22rem)] flex-col border-border bg-background p-4 shadow-xl focus:outline-none', side === 'left' ? 'left-0 border-r' : 'right-0 border-l', props.class)"
    >
      <DialogTitle class="sr-only">辅助面板</DialogTitle>
      <DialogDescription class="sr-only">查看当前辅助面板内容。</DialogDescription>
      <slot />
    </DialogContent>
  </DialogPortal>
</template>
