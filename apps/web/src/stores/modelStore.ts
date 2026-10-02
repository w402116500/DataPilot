import { ref } from "vue";
import { defineStore } from "pinia";

import {
  activateModelProfile,
  createModelProfile,
  deleteModelProfile,
  listModelProfiles,
  testModelProfile,
  updateModelProfile,
} from "@/api/modelProfiles";
import type {
  DeleteResult,
  ModelProfile,
  ModelProfileCreate,
  ModelProfileTestResult,
  ModelProfileUpdate,
} from "@/api/types";

export const useModelStore = defineStore("model", () => {
  const items = ref<ModelProfile[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  function replace(item: ModelProfile): void {
    items.value = [item, ...items.value.filter((candidate) => candidate.id !== item.id)];
  }

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      items.value = (await listModelProfiles()).items;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : "模型配置读取失败";
    } finally {
      loading.value = false;
    }
  }

  async function create(payload: ModelProfileCreate): Promise<ModelProfile> {
    const item = await createModelProfile(payload);
    replace(item);
    return item;
  }

  async function update(profileId: string, payload: ModelProfileUpdate): Promise<ModelProfile> {
    const item = await updateModelProfile(profileId, payload);
    replace(item);
    return item;
  }

  async function test(profileId: string): Promise<ModelProfileTestResult> {
    return testModelProfile(profileId);
  }

  async function activate(profileId: string): Promise<ModelProfile> {
    const active = await activateModelProfile(profileId);
    items.value = items.value.map((item) => ({ ...item, is_active: item.id === active.id }));
    replace(active);
    return active;
  }

  async function remove(profileId: string): Promise<DeleteResult> {
    const result = await deleteModelProfile(profileId);
    items.value = items.value.filter((item) => item.id !== profileId);
    return result;
  }

  return { items, loading, error, load, create, update, test, activate, remove };
});
