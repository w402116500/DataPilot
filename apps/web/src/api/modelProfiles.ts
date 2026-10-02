import { requestJson } from "./client";
import type {
  DeleteResult,
  ModelProfile,
  ModelProfileCreate,
  ModelProfileTestResult,
  ModelProfileUpdate,
  PageResult,
} from "./types";

function pageQuery(page: number, pageSize: number): string {
  return `?page=${page}&page_size=${pageSize}`;
}

export function listModelProfiles(page = 1, pageSize = 20): Promise<PageResult<ModelProfile>> {
  return requestJson<PageResult<ModelProfile>>(`/model-profiles${pageQuery(page, pageSize)}`);
}

export function createModelProfile(payload: ModelProfileCreate): Promise<ModelProfile> {
  return requestJson<ModelProfile>("/model-profiles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getModelProfile(profileId: string): Promise<ModelProfile> {
  return requestJson<ModelProfile>(`/model-profiles/${encodeURIComponent(profileId)}`);
}

export function updateModelProfile(
  profileId: string,
  payload: ModelProfileUpdate,
): Promise<ModelProfile> {
  return requestJson<ModelProfile>(`/model-profiles/${encodeURIComponent(profileId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function testModelProfile(profileId: string): Promise<ModelProfileTestResult> {
  return requestJson<ModelProfileTestResult>(`/model-profiles/${encodeURIComponent(profileId)}/test`, {
    method: "POST",
  });
}

export function activateModelProfile(profileId: string): Promise<ModelProfile> {
  return requestJson<ModelProfile>(
    `/model-profiles/${encodeURIComponent(profileId)}/activate`,
    { method: "POST" },
  );
}

export function deleteModelProfile(profileId: string): Promise<DeleteResult> {
  return requestJson<DeleteResult>(`/model-profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
}
