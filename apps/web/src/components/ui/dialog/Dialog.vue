<script setup lang="ts">
import type { DialogRootEmits, DialogRootProps } from "reka-ui";
import { DialogRoot, useForwardPropsEmits } from "reka-ui";

const props = defineProps<DialogRootProps>();
const emit = defineEmits<DialogRootEmits>();
const forwarded = useForwardPropsEmits(props, emit);
</script>

<template><DialogRoot v-bind="forwarded"><slot /></DialogRoot></template>
