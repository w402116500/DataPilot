import { ref } from "vue";
import { defineStore } from "pinia";

import { downloadArtifact, getArtifactContent } from "@/api/artifacts";
import { listRunArtifacts } from "@/api/runs";
import type { BinaryResponse } from "@/api/client";
import type { RunArtifact } from "@/api/types";

export const useArtifactStore = defineStore("artifact", () => {
  const items = ref<RunArtifact[]>([]);
  const contentById = ref<Record<string, BinaryResponse>>({});
  const loading = ref(false);
  const error = ref<string | null>(null);

  async function loadForRun(runId: string): Promise<RunArtifact[]> {
    loading.value = true;
    error.value = null;
    try {
      items.value = await listRunArtifacts(runId);
      return items.value;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : "产物读取失败";
      throw caught;
    } finally {
      loading.value = false;
    }
  }

  async function readContent(artifactId: string, signal?: AbortSignal): Promise<BinaryResponse> {
    const content = await getArtifactContent(artifactId, signal);
    contentById.value = { ...contentById.value, [artifactId]: content };
    return content;
  }

  async function download(artifactId: string): Promise<BinaryResponse> {
    return downloadArtifact(artifactId);
  }

  return { items, contentById, loading, error, loadForRun, readContent, download };
});
