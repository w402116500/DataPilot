<script setup lang="ts">
import type { TabsTriggerProps } from "reka-ui";
import type { HTMLAttributes } from "vue";
import { reactiveOmit } from "@vueuse/core";
import { TabsTrigger, useForwardProps } from "reka-ui";

import { cn } from "@/lib/utils";

const props = defineProps<TabsTriggerProps & { class?: HTMLAttributes["class"] }>();
const delegatedProps = reactiveOmit(props, "class");
const forwardedProps = useForwardProps(delegatedProps);
</script>

<template>
  <TabsTrigger v-bind="forwardedProps" :class="cn('inline-flex h-7 items-center justify-center whitespace-nowrap rounded-md px-2 text-xs font-medium transition-all data-[state=active]:bg-workspace-surface data-[state=active]:text-foreground data-[state=active]:shadow-sm', props.class)">
    <span class="truncate"><slot /></span>
  </TabsTrigger>
</template>
