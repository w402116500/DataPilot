import { requestJson } from "./client";
import type {
  DataLinkConsumption,
  DataLinkConsumptionList,
  Run,
  RunArtifact,
  RunCancel,
  RunCancelRequest,
  SqlAudit,
  TraceDag,
  ToolCall,
} from "./types";

export function getRun(runId: string): Promise<Run> {
  return requestJson<Run>(`/runs/${encodeURIComponent(runId)}`);
}

export function cancelRun(runId: string, payload: RunCancelRequest = {}): Promise<RunCancel> {
  return requestJson<RunCancel>(`/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listRunToolCalls(runId: string): Promise<ToolCall[]> {
  return requestJson<ToolCall[]>(`/runs/${encodeURIComponent(runId)}/tool-calls`);
}

export function listRunSqlAudits(runId: string): Promise<SqlAudit[]> {
  return requestJson<SqlAudit[]>(`/runs/${encodeURIComponent(runId)}/sql-audits`);
}

export function listRunArtifacts(runId: string): Promise<RunArtifact[]> {
  return requestJson<RunArtifact[]>(`/runs/${encodeURIComponent(runId)}/artifacts`);
}

export function listRunDatalinkConsumptions(runId: string): Promise<DataLinkConsumptionList> {
  return requestJson<DataLinkConsumptionList>(`/runs/${encodeURIComponent(runId)}/datalink-consumptions`);
}

export function getRunDatalinkConsumption(runId: string, consumptionId: string): Promise<DataLinkConsumption> {
  return requestJson<DataLinkConsumption>(
    `/runs/${encodeURIComponent(runId)}/datalink-consumptions/${encodeURIComponent(consumptionId)}`,
  );
}

export function getTraceDag(runId: string): Promise<TraceDag> {
  return requestJson<TraceDag>(`/runs/${encodeURIComponent(runId)}/trace-dag`);
}
