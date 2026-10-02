<script setup lang="ts">
import type { TabsContentProps } from "reka-ui";
import type { HTMLAttributes } from "vue";
import { reactiveOmit } from "@vueuse/core";
import { TabsContent } from "reka-ui";

import { cn } from "@/lib/utils";

const props = defineProps<TabsContentProps & { class?: HTMLAttributes["class"] }>();
const delegatedProps = reactiveOmit(props, "class");
</script>

<template><TabsContent v-bind="delegatedProps" :class="cn('mt-3 min-w-0', props.class)"><slot /></TabsContent></template>
