<script setup lang="ts">
import type { DataLinkSemanticContext } from "@/api/types";
import { datalinkConfidence } from "@/lib/datalinkDisplay";

defineProps<{ context: DataLinkSemanticContext }>();
</script>

<template>
  <div class="semantic-result">
    <p class="result-counts">核对清单 · 字段 {{ context.fields.length }} · 关系 {{ context.relationships.length }} · Join {{ context.join_paths.length }} · 警告 {{ context.warnings.length }}</p>
    <section>
      <h4>字段语义</h4>
      <p v-if="!context.fields.length" class="empty">没有返回匹配字段</p>
      <div v-for="field in context.fields" :key="`${field.table}.${field.column}`" class="field-result">
        <div class="field-heading"><strong>{{ field.table }}.{{ field.column }}</strong><span>{{ field.semantic_type ?? '语义类型未记录' }}</span></div>
        <p v-if="field.description">{{ field.description }}</p>
        <p v-if="field.aliases.length" class="muted">别名：{{ field.aliases.join('、') }}</p>
        <div v-for="(mapping, index) in field.semantic_mappings" :key="index" class="mapping">
          <div><span class="muted">属性</span><strong>{{ mapping.concept.name }}</strong><span class="muted">{{ datalinkConfidence(mapping.field_to_concept_confidence) }}</span></div>
          <p v-if="mapping.concept.description">{{ mapping.concept.description }}</p>
          <p v-if="mapping.concept.aliases.length" class="muted">别名：{{ mapping.concept.aliases.join('、') }}</p>
          <div v-for="(entity, entityIndex) in mapping.entities" :key="entityIndex"><span class="muted">实体</span><strong>{{ entity.entity.name }}</strong><span class="muted">{{ datalinkConfidence(entity.entity_to_concept_confidence) }}</span><p v-if="entity.entity.description">{{ entity.entity.description }}</p></div>
        </div>
      </div>
    </section>
    <section>
      <h4>可连接关系</h4>
      <p v-if="!context.relationships.length" class="empty">没有返回可连接关系</p>
      <div v-for="(relation, index) in context.relationships" :key="index" class="relation-result">
        <strong>{{ relation.source_table }}.{{ relation.source_column }} → {{ relation.target_table }}.{{ relation.target_column }}</strong>
        <p class="muted">{{ relation.provenance === 'manual' ? '人工候选 · 未核验' : relation.edge_type === 'foreign_key' ? '数据库外键' : '候选关联' }} · {{ relation.confidence === null ? '无统计分数' : datalinkConfidence(relation.confidence) }}</p>
        <p v-if="relation.evidence">{{ relation.evidence.summary }}</p>
      </div>
    </section>
    <section>
      <h4>Join 路径</h4>
      <p v-if="!context.join_paths.length" class="empty">没有返回 Join 路径</p>
      <div v-for="(path, index) in context.join_paths" :key="index" class="relation-result">
        <strong>{{ path.tables.join(' → ') }}</strong>
        <p v-for="(step, stepIndex) in path.steps" :key="stepIndex">{{ step.source_table }}.{{ step.source_column }} = {{ step.target_table }}.{{ step.target_column }}</p>
        <p class="muted">{{ datalinkConfidence(path.confidence) }} · {{ path.evidence }}</p>
      </div>
    </section>
    <section>
      <h4>警告</h4>
      <p v-if="!context.warnings.length" class="empty">没有警告</p>
      <ul v-else class="warnings"><li v-for="warning in context.warnings" :key="warning">{{ warning }}</li></ul>
    </section>
    <details><summary>语义返回 JSON</summary><pre>{{ JSON.stringify(context, null, 2) }}</pre></details>
  </div>
</template>

<style scoped>
.semantic-result { display: grid; gap: 22px; min-width: 0; font-size: 12px; line-height: 1.6; }
.result-counts { margin: 0 0 4px; font-size: 11px; color: var(--workspace-text-muted); }
h4 { font-size: 13px; margin: 0 0 8px; }.field-result, .relation-result { padding: 12px 0; border-bottom: 1px solid var(--workspace-border); overflow-wrap: anywhere; }p { margin: 4px 0; }.field-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; }.field-heading > span, .muted, .empty { color: var(--workspace-text-muted); }
.mapping { margin-top: 10px; border-left: 2px solid var(--workspace-border); padding-left: 12px; }.mapping > div { display: flex; flex-wrap: wrap; gap: 6px 10px; }.mapping p { flex-basis: 100%; }summary { cursor: pointer; color: var(--workspace-text-muted); }pre { max-height: 380px; overflow: auto; padding: 12px; background: var(--workspace-surface-inset); border-radius: 6px; font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }.warnings { padding-left: 18px; color: var(--workspace-state-warning); }
</style>
