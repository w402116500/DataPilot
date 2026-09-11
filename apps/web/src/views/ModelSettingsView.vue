<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import {
  Check,
  CircleAlert,
  KeyRound,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  ShieldCheck,
  TestTube2,
  Trash2,
} from "@lucide/vue";

import type { ModelProfile, ModelProfileCreate, ModelProfileUpdate } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useModelStore } from "@/stores/modelStore";

const models = useModelStore();

const selectedProfileId = ref("");
const formOpen = ref(false);
const deleteOpen = ref(false);
const formMode = ref<"create" | "edit">("create");
const busyAction = ref<string | null>(null);
const actionError = ref<string | null>(null);
const testMessage = ref<string | null>(null);
const formError = ref<string | null>(null);
const secretKey = ref("");
const form = reactive({
  name: "",
  provider: "openai-compatible",
  modelName: "",
  baseUrl: "",
  temperature: "0",
  runTimeoutSeconds: "600",
  contextWindowTokens: "",
});

const selectedProfile = computed(
  () => models.items.find((profile) => profile.id === selectedProfileId.value) ?? null,
);
const isCreate = computed(() => formMode.value === "create");
const canSubmit = computed(
  () =>
    form.name.trim().length > 0 &&
    form.modelName.trim().length > 0 &&
    form.baseUrl.trim().length > 0 &&
    (isCreate.value ? secretKey.value.trim().length > 0 : true) &&
    busyAction.value === null,
);

function statusLabel(profile: ModelProfile): string {
  if (profile.is_active) return "当前使用";
  if (profile.status === "tested") return "已测试";
  if (profile.status === "failed") return "测试失败";
  return "未测试";
}

function statusClass(profile: ModelProfile): string {
  if (profile.is_active || profile.status === "tested") return "state-success";
  if (profile.status === "failed") return "state-error";
  return "state-muted";
}

function toolCallingLabel(profile: ModelProfile): string {
  if (profile.tool_calling_supported === true) return "支持";
  if (profile.tool_calling_supported === false) return "不支持";
  return "未探测";
}

function finalOutputModeLabel(profile: ModelProfile): string {
  switch (profile.final_output_mode) {
    case "markdown":
      return "直接 Markdown";
    case "submit_answer":
      return "答案工具兜底";
    case "json_schema":
      return "历史 JSON Schema";
    case "json_object":
      return "历史 JSON Object";
    default:
      return "未探测";
  }
}

function safeError(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message ? caught.message : fallback;
}

function clearSecret(): void {
  secretKey.value = "";
}

function resetForm(): void {
  form.name = "";
  form.provider = "openai-compatible";
  form.modelName = "";
  form.baseUrl = "";
  form.temperature = "0";
  form.runTimeoutSeconds = "600";
  form.contextWindowTokens = "";
  clearSecret();
  formError.value = null;
}

function openCreate(): void {
  formMode.value = "create";
  resetForm();
  formOpen.value = true;
}

function openEdit(profile: ModelProfile): void {
  selectedProfileId.value = profile.id;
  formMode.value = "edit";
  form.name = profile.name;
  form.provider = profile.provider;
  form.modelName = profile.model_name;
  form.baseUrl = profile.base_url;
  form.temperature = String(profile.temperature);
  form.runTimeoutSeconds = String(profile.run_timeout_seconds);
  form.contextWindowTokens = profile.context_window_tokens === null ? "" : String(profile.context_window_tokens);
  clearSecret();
  formError.value = null;
  formOpen.value = true;
}

function validateForm(): boolean {
  const temperature = Number(form.temperature);
  const timeout = Number(form.runTimeoutSeconds);
  const contextWindow = form.contextWindowTokens.trim() === "" ? null : Number(form.contextWindowTokens);
  if (!Number.isFinite(temperature) || temperature < 0 || temperature > 2) {
    formError.value = "温度需要在 0 到 2 之间。";
    return false;
  }
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 600) {
    formError.value = "运行超时需要是 1 到 600 秒的整数。";
    return false;
  }
  if (contextWindow !== null && (!Number.isInteger(contextWindow) || contextWindow < 1024 || contextWindow > 1_000_000)) {
    formError.value = "上下文窗口需要是 1,024 到 1,000,000 之间的整数，或留空使用服务默认值。";
    return false;
  }
  try {
    const parsed = new URL(form.baseUrl.trim());
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("invalid protocol");
  } catch {
    formError.value = "基础地址需要是 http 或 https 地址。";
    return false;
  }
  return true;
}

async function submitForm(): Promise<void> {
  formError.value = null;
  if (!canSubmit.value || !validateForm()) {
    if (!canSubmit.value && !formError.value) formError.value = "请把必填项填写完整。";
    clearSecret();
    return;
  }
  busyAction.value = "form";
  actionError.value = null;
  try {
    const temperature = Number(form.temperature);
    const runTimeoutSeconds = Number(form.runTimeoutSeconds);
    const contextWindowTokens = form.contextWindowTokens.trim() === "" ? null : Number(form.contextWindowTokens);
    if (isCreate.value) {
      const payload: ModelProfileCreate = {
        name: form.name.trim(),
        provider: form.provider.trim() || undefined,
        model_name: form.modelName.trim(),
        base_url: form.baseUrl.trim(),
        api_key: secretKey.value.trim(),
        temperature,
        run_timeout_seconds: runTimeoutSeconds,
        context_window_tokens: contextWindowTokens,
      };
      const created = await models.create(payload);
      selectedProfileId.value = created.id;
    } else {
      const payload: ModelProfileUpdate = {
        name: form.name.trim(),
        provider: form.provider.trim(),
        model_name: form.modelName.trim(),
        base_url: form.baseUrl.trim(),
        temperature,
        run_timeout_seconds: runTimeoutSeconds,
        context_window_tokens: contextWindowTokens,
      };
      if (secretKey.value.trim()) payload.api_key = secretKey.value.trim();
      await models.update(selectedProfileId.value, payload);
    }
    formOpen.value = false;
  } catch (caught) {
    actionError.value = safeError(caught, isCreate.value ? "创建模型配置失败" : "更新模型配置失败");
  } finally {
    clearSecret();
    busyAction.value = null;
  }
}

async function testSelected(): Promise<void> {
  const profile = selectedProfile.value;
  if (!profile) return;
  busyAction.value = `test:${profile.id}`;
  actionError.value = null;
  testMessage.value = null;
  try {
    const result = await models.test(profile.id);
    testMessage.value = result.message;
    await models.load();
  } catch (caught) {
    actionError.value = safeError(caught, "模型配置测试失败");
  } finally {
    busyAction.value = null;
  }
}

async function activateSelected(): Promise<void> {
  const profile = selectedProfile.value;
  if (!profile) return;
  busyAction.value = `activate:${profile.id}`;
  actionError.value = null;
  try {
    await models.activate(profile.id);
    testMessage.value = "已将这份配置设为当前模型。";
  } catch (caught) {
    actionError.value = safeError(caught, "激活模型配置失败");
  } finally {
    busyAction.value = null;
  }
}

async function removeSelected(): Promise<void> {
  const profile = selectedProfile.value;
  if (!profile) return;
  busyAction.value = `delete:${profile.id}`;
  actionError.value = null;
  try {
    await models.remove(profile.id);
    deleteOpen.value = false;
    selectedProfileId.value = models.items[0]?.id ?? "";
    testMessage.value = null;
  } catch (caught) {
    actionError.value = safeError(caught, "删除模型配置失败");
  } finally {
    busyAction.value = null;
  }
}

async function refreshProfiles(): Promise<void> {
  busyAction.value = "list";
  actionError.value = null;
  try {
    await models.load();
    if (!models.items.some((profile) => profile.id === selectedProfileId.value)) {
      selectedProfileId.value = models.items[0]?.id ?? "";
    }
  } catch (caught) {
    actionError.value = safeError(caught, "刷新模型配置失败");
  } finally {
    busyAction.value = null;
  }
}

onMounted(refreshProfiles);
</script>

<template>
  <section class="model-settings" aria-label="模型设置">
    <header class="model-header">
      <div>
        <p class="section-kicker">模型运行</p>
        <h1>模型设置</h1>
        <p>配置一个可用的 OpenAI-compatible 模型，Run 创建时会固定当时的配置。</p>
      </div>
      <div class="header-actions"><Button variant="outline" :disabled="busyAction !== null" @click="refreshProfiles"><RefreshCw :size="15" :class="{ spinning: busyAction === 'list' }" />刷新</Button><Button :disabled="busyAction !== null" @click="openCreate"><Plus :size="15" />新增模型</Button></div>
    </header>

    <p v-if="actionError || models.error" class="page-error">{{ actionError ?? models.error }}</p>

    <div class="models-layout">
      <aside class="profile-list" aria-label="模型配置列表">
        <div class="list-heading"><span>配置列表</span><small>{{ models.items.length }}</small></div>
        <div v-if="models.loading && models.items.length === 0" class="list-empty">正在读取模型配置...</div>
        <div v-else-if="models.items.length === 0" class="list-empty"><Settings2 :size="24" /><strong>还没有模型配置</strong><span>新增一份配置后，工作台才能创建 Run。</span><Button size="sm" @click="openCreate"><Plus :size="14" />新增模型</Button></div>
        <div v-else class="profile-items">
          <button v-for="profile in models.items" :key="profile.id" type="button" class="profile-row" :class="{ active: profile.id === selectedProfileId }" @click="selectedProfileId = profile.id; testMessage = null; actionError = null">
            <span class="profile-name"><Settings2 :size="15" />{{ profile.name }}</span>
            <span class="profile-model">{{ profile.provider }} · {{ profile.model_name }}</span>
            <span class="profile-row-bottom"><Badge variant="outline" :class="statusClass(profile)">{{ statusLabel(profile) }}</Badge><span v-if="profile.has_api_key" class="key-state"><KeyRound :size="12" />已配置密钥</span></span>
          </button>
        </div>
      </aside>

      <main class="profile-detail">
        <div v-if="selectedProfile === null" class="detail-empty"><Settings2 :size="30" stroke-width="1.4" /><h2>选择或新增模型</h2><p>模型配置只显示安全投影。密钥永远不会出现在列表、Store 或 API 响应里。</p><Button @click="openCreate"><Plus :size="15" />新增模型</Button></div>
        <template v-else>
          <header class="detail-header">
            <div><div class="detail-title-row"><h2>{{ selectedProfile.name }}</h2><Badge v-if="selectedProfile.is_active" variant="outline" class="state-success"><Check :size="12" />当前使用</Badge><Badge v-else variant="outline" :class="statusClass(selectedProfile)">{{ statusLabel(selectedProfile) }}</Badge></div><p>{{ selectedProfile.provider }} · {{ selectedProfile.model_name }}</p></div>
            <div class="detail-actions"><Button variant="outline" size="sm" :disabled="busyAction !== null || !selectedProfile.has_api_key" @click="testSelected"><TestTube2 :size="14" />{{ busyAction === `test:${selectedProfile.id}` ? '正在测试' : '测试配置' }}</Button><Button v-if="!selectedProfile.is_active" variant="outline" size="sm" :disabled="busyAction !== null || !selectedProfile.has_api_key" @click="activateSelected"><Play :size="14" />激活</Button><Button variant="outline" size="sm" :disabled="busyAction !== null" @click="openEdit(selectedProfile)"><Settings2 :size="14" />编辑</Button><Button variant="destructive" size="sm" :disabled="busyAction !== null" @click="deleteOpen = true"><Trash2 :size="14" />删除</Button></div>
          </header>
          <p v-if="testMessage" class="detail-notice state-success"><ShieldCheck :size="15" />{{ testMessage }}</p>
          <p v-else-if="!selectedProfile.has_api_key" class="detail-notice state-warning"><CircleAlert :size="15" />这份配置没有密钥，不能测试或激活。编辑配置补充密钥后再试。</p>
          <div class="profile-facts"><div><span>服务地址</span><strong>{{ selectedProfile.base_url }}</strong></div><div><span>温度</span><strong>{{ selectedProfile.temperature }}</strong></div><div><span>Run 超时</span><strong>{{ selectedProfile.run_timeout_seconds }} 秒</strong></div><div><span>上下文窗口</span><strong>{{ selectedProfile.context_window_tokens === null ? '服务默认值' : `${selectedProfile.context_window_tokens.toLocaleString()} tokens` }}</strong></div><div><span>密钥状态</span><strong>{{ selectedProfile.has_api_key ? '已配置（只显示状态）' : '未配置' }}</strong></div><div><span>工具调用</span><strong>{{ toolCallingLabel(selectedProfile) }}</strong></div><div><span>最终答案格式</span><strong>{{ finalOutputModeLabel(selectedProfile) }}</strong></div></div>
          <div class="security-note"><ShieldCheck :size="16" /><div><strong>密钥边界</strong><p>密钥只在创建或编辑表单里使用一次。提交结束后会清空；这里不会显示密钥内容，也不会写入浏览器存储。</p></div></div>
        </template>
      </main>
    </div>

    <Dialog v-model:open="formOpen">
      <DialogContent class="model-dialog">
        <DialogHeader><DialogTitle>{{ isCreate ? '新增模型配置' : '编辑模型配置' }}</DialogTitle><DialogDescription>{{ isCreate ? '提交后服务端会加密保存密钥，列表只显示是否配置。' : '不填写密钥会保留当前密钥，填写新值才会替换它。' }}</DialogDescription></DialogHeader>
        <form class="model-form" @submit.prevent="submitForm">
          <label for="model-name">名称</label><Input id="model-name" v-model="form.name" maxlength="120" />
          <label for="model-provider">提供方</label><Input id="model-provider" v-model="form.provider" maxlength="80" placeholder="openai-compatible" />
          <label for="model-model-name">模型名</label><Input id="model-model-name" v-model="form.modelName" maxlength="120" placeholder="例如：gpt-4o-mini" />
          <label for="model-base-url">基础地址</label><Input id="model-base-url" v-model="form.baseUrl" type="url" placeholder="https://api.example.com/v1" />
          <label for="model-api-key">API Key{{ isCreate ? '' : '（可选）' }}</label><Input id="model-api-key" v-model="secretKey" type="password" autocomplete="new-password" placeholder="只在本次提交中使用" />
          <div class="form-grid"><div><label for="model-temperature">温度</label><Input id="model-temperature" v-model="form.temperature" type="number" min="0" max="2" step="0.1" /></div><div><label for="model-timeout">Run 超时（秒）</label><Input id="model-timeout" v-model="form.runTimeoutSeconds" type="number" min="1" max="600" step="1" /></div><div><label for="model-context-window">上下文窗口（tokens，可选）</label><Input id="model-context-window" v-model="form.contextWindowTokens" type="number" min="1024" max="1000000" step="1" placeholder="留空使用服务默认值" /></div></div>
          <p v-if="formError" class="form-error">{{ formError }}</p>
          <DialogFooter><Button type="button" variant="outline" :disabled="busyAction === 'form'" @click="formOpen = false; clearSecret()">取消</Button><Button type="submit" :disabled="!canSubmit">{{ busyAction === 'form' ? '正在保存' : '保存配置' }}</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="deleteOpen">
      <DialogContent><DialogHeader><DialogTitle>删除模型配置？</DialogTitle><DialogDescription>删除后新的 Run 不能再使用它；已经固定快照的历史 Run 仍按自己的记录回放。这个操作也会回收服务器保存的密钥。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" :disabled="busyAction?.startsWith('delete:')" @click="deleteOpen = false">取消</Button><Button variant="destructive" :disabled="busyAction?.startsWith('delete:')" @click="removeSelected"><Trash2 :size="14" />{{ busyAction?.startsWith('delete:') ? '正在删除' : '确认删除' }}</Button></DialogFooter></DialogContent>
    </Dialog>
  </section>
</template>

<style scoped>
.model-settings { min-height: calc(100vh - 48px); padding: 24px; }.model-header, .detail-header, .detail-title-row, .header-actions, .detail-actions, .profile-name, .profile-row-bottom, .detail-notice, .security-note { display: flex; align-items: center; }.model-header { max-width: 1440px; justify-content: space-between; gap: 20px; margin: 0 auto 18px; }.section-kicker { margin: 0 0 5px; color: var(--muted-foreground); font-size: 11px; font-weight: 700; }.model-header h1 { margin: 0; font-size: 22px; line-height: 1.25; }.model-header p:last-child { margin: 7px 0 0; color: var(--muted-foreground); font-size: 13px; line-height: 1.5; }.header-actions, .detail-actions { flex-wrap: wrap; gap: 7px; }.page-error { max-width: 1440px; margin: 0 auto 14px; border: 1px solid #f0bab5; background: #fff7f6; padding: 9px 11px; color: var(--destructive); font-size: 13px; }
.models-layout { display: grid; max-width: 1440px; min-height: 540px; grid-template-columns: minmax(270px, 330px) minmax(0, 1fr); margin: 0 auto; overflow: hidden; border: 1px solid var(--border); border-radius: 6px; background: var(--card); }.profile-list { min-width: 0; border-right: 1px solid var(--border); background: #fbfbfa; }.list-heading { display: flex; justify-content: space-between; border-bottom: 1px solid var(--border); padding: 14px; color: #575752; font-size: 11px; font-weight: 700; }.list-heading small { color: var(--muted-foreground); font-weight: 500; }.profile-items { display: grid; gap: 2px; padding: 8px; }.profile-row { display: grid; width: 100%; gap: 5px; border: 0; border-radius: 4px; background: transparent; padding: 10px 8px; color: inherit; cursor: pointer; text-align: left; }.profile-row:hover { background: var(--accent); }.profile-row.active { background: #e8e8e4; }.profile-name { min-width: 0; gap: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 650; }.profile-model { overflow: hidden; color: var(--muted-foreground); text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }.profile-row-bottom { justify-content: space-between; gap: 5px; }.key-state { display: inline-flex; align-items: center; gap: 3px; color: var(--muted-foreground); font-size: 10px; }.list-empty { display: grid; justify-items: center; gap: 8px; padding: 32px 18px; color: var(--muted-foreground); text-align: center; font-size: 12px; line-height: 1.5; }.list-empty strong { color: var(--foreground); font-size: 13px; }.list-empty :deep(button) { margin-top: 4px; }
.profile-detail { min-width: 0; background: var(--card); }.detail-empty { display: grid; min-height: 540px; place-content: center; justify-items: center; gap: 10px; padding: 24px; color: var(--muted-foreground); text-align: center; }.detail-empty h2, .detail-empty p { margin: 0; }.detail-empty h2 { color: var(--foreground); font-size: 17px; }.detail-empty p { max-width: 380px; font-size: 13px; line-height: 1.6; }.detail-header { justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--border); padding: 18px 20px; }.detail-title-row { flex-wrap: wrap; gap: 8px; }.detail-title-row h2 { margin: 0; font-size: 17px; }.detail-title-row :deep(.inline-flex) { gap: 4px; }.detail-header p { margin: 6px 0 0; color: var(--muted-foreground); font-size: 12px; }.detail-notice { gap: 7px; margin: 14px 20px 0; border-left: 2px solid var(--border); background: #fafaf9; padding: 9px 10px; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; }.detail-notice.state-success { border-left-color: var(--success); }.detail-notice.state-warning { border-left-color: var(--warning); }.profile-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; margin: 20px; border: 1px solid var(--border); background: var(--border); }.profile-facts div { display: grid; min-width: 0; gap: 6px; background: #fcfcfb; padding: 13px; }.profile-facts span { color: var(--muted-foreground); font-size: 11px; }.profile-facts strong { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; font-weight: 550; }.security-note { gap: 9px; margin: 0 20px; border-top: 1px solid var(--border); padding-top: 16px; color: var(--muted-foreground); }.security-note svg { flex: 0 0 auto; color: var(--success); }.security-note strong { color: var(--foreground); font-size: 12px; }.security-note p { margin: 4px 0 0; font-size: 12px; line-height: 1.55; }
.model-dialog { max-height: 90vh; overflow: auto; }.model-form { display: grid; gap: 7px; }.model-form > label, .model-form .form-grid label { color: #575752; font-size: 11px; font-weight: 700; }.model-form > :not(label):not(.form-grid):not(.form-error) { margin-bottom: 4px; }.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }.form-grid > div { display: grid; gap: 7px; }.form-error { margin: 0; color: var(--destructive); font-size: 12px; }.spinning { animation: spin 1s linear infinite; }@keyframes spin { to { transform: rotate(360deg); } }.state-success { color: var(--success); }.state-warning { color: var(--warning); }.state-error { color: var(--destructive); }.state-muted { color: var(--muted-foreground); }
@media (max-width: 840px) { .model-settings { padding: 12px; }.models-layout { min-height: 0; grid-template-columns: minmax(0, 1fr); }.profile-list { border-right: 0; border-bottom: 1px solid var(--border); }.profile-items { max-height: 230px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); overflow: auto; }.detail-empty { min-height: 360px; }.detail-header { padding: 16px; }.detail-notice { margin-left: 16px; margin-right: 16px; }.profile-facts { margin: 16px; }.security-note { margin: 0 16px; } }
@media (max-width: 560px) { .model-settings { padding: 10px; }.model-header { align-items: flex-start; flex-direction: column; gap: 12px; }.header-actions { width: 100%; }.header-actions :deep(button) { flex: 1; }.profile-items { grid-template-columns: 1fr; }.detail-header { align-items: flex-start; flex-direction: column; }.detail-actions { width: 100%; }.detail-actions :deep(button) { flex: 1; }.profile-facts { grid-template-columns: 1fr; }.form-grid { grid-template-columns: 1fr; } }
</style>
