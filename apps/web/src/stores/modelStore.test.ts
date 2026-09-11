import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import type { ModelProfile } from "@/api/types";

const api = vi.hoisted(() => ({
  activateModelProfile: vi.fn(),
  createModelProfile: vi.fn(),
  deleteModelProfile: vi.fn(),
  listModelProfiles: vi.fn(),
  testModelProfile: vi.fn(),
  updateModelProfile: vi.fn(),
}));

vi.mock("@/api/modelProfiles", () => api);

import { useModelStore } from "./modelStore";

function profile(): ModelProfile {
  return {
    id: "profile_1",
    name: "演示模型",
    provider: "openai-compatible",
    model_name: "demo-model",
    base_url: "https://example.com/v1",
    temperature: 0,
    run_timeout_seconds: 600,
    context_window_tokens: null,
    status: "created",
    is_active: false,
    has_api_key: true,
    tool_calling_supported: null,
    final_output_mode: null,
    capability_contract_version: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
  };
}

describe("modelStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it("accepts an API key only as a command and never stores it in the profile projection", async () => {
    const item = profile();
    api.createModelProfile.mockResolvedValue(item);
    api.updateModelProfile.mockResolvedValue({ ...item, name: "更新后的模型" });
    const store = useModelStore();

    await store.create({
      name: item.name,
      model_name: item.model_name,
      base_url: item.base_url,
      api_key: "secret-only-for-request",
    });
    await store.update(item.id, { name: "更新后的模型", api_key: "replacement-secret" });

    expect(api.createModelProfile).toHaveBeenCalledWith(expect.objectContaining({ api_key: "secret-only-for-request" }));
    expect(api.updateModelProfile).toHaveBeenCalledWith(item.id, expect.objectContaining({ api_key: "replacement-secret" }));
    expect(store.items).toEqual([{ ...item, name: "更新后的模型" }]);
    expect(JSON.stringify(store.items)).not.toContain("secret-only-for-request");
    expect(JSON.stringify(store.items)).not.toContain("replacement-secret");
  });
});
