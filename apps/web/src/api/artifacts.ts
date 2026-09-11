import { requestBinary, requestJson } from "./client";
import type { RunArtifact } from "./types";

export function getArtifact(artifactId: string): Promise<RunArtifact> {
  return requestJson<RunArtifact>(`/artifacts/${encodeURIComponent(artifactId)}`);
}

export function getArtifactContent(artifactId: string, signal?: AbortSignal) {
  return requestBinary(`/artifacts/${encodeURIComponent(artifactId)}/content`, { signal });
}

export function downloadArtifact(artifactId: string) {
  return requestBinary(`/artifacts/${encodeURIComponent(artifactId)}/download`);
}
