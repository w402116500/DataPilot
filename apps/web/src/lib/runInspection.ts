import type { RunArtifact, SqlAudit, ToolCall } from "@/api/types";
import { toolLabel } from "./runActivity";

export interface RunInspectionGroup {
  id: string;
  title: string;
  tool: ToolCall | null;
  audits: SqlAudit[];
  artifacts: RunArtifact[];
}

export function buildRunInspectionGroups(
  runId: string,
  tools: readonly ToolCall[],
  audits: readonly SqlAudit[],
  artifacts: readonly RunArtifact[],
): RunInspectionGroup[] {
  let sqlNumber = 0;
  const groups: RunInspectionGroup[] = tools.filter((tool) => tool.run_id === runId).map((tool) => ({
    id: `tool:${tool.id}`,
    title: tool.tool_name === "run_sql_readonly" ? `SQL 查询 ${++sqlNumber}` : toolLabel(tool.tool_name),
    tool,
    audits: [],
    artifacts: [],
  }));
  for (const audit of audits.filter((item) => item.run_id === runId)) {
    const group = groups.find((item) => item.tool?.id === audit.tool_call_id);
    if (group) group.audits.push(audit);
    else groups.push({ id: `audit:${audit.id}`, title: `SQL 查询 ${++sqlNumber}`, tool: null, audits: [audit], artifacts: [] });
  }
  for (const artifact of artifacts.filter((item) => item.run_id === runId)) {
    const toolGroup = groups.find((item) => item.tool?.id === artifact.tool_call_id);
    const auditGroups = groups.filter((item) => item.audits.some((audit) => audit.artifact_id === artifact.id));
    // Never infer provenance from a shared title or an absent identifier.
    const group = toolGroup ?? (auditGroups.length === 1 ? auditGroups[0] : undefined);
    if (group) group.artifacts.push(artifact);
    else groups.push({ id: `artifact:${artifact.id}`, title: artifact.title, tool: null, audits: [], artifacts: [artifact] });
  }
  return groups;
}

export function inspectionGroupForRef(groups: readonly RunInspectionGroup[], ref: string): RunInspectionGroup | undefined {
  return groups.find((group) => group.id === ref
    || (group.tool !== null && ref === group.tool.id)
    || group.audits.some((audit) => ref === audit.id || ref === `audit:${audit.id}`)
    || group.artifacts.some((artifact) => ref === artifact.id || ref === `artifact:${artifact.id}`));
}

export function inspectionGroupSummary(group: RunInspectionGroup): string {
  const tables = [...new Set(group.audits.flatMap((audit) => audit.referenced_tables))];
  const results = group.artifacts.length ? `${group.artifacts.length} 个结果` : "暂无产物";
  return tables.length ? `${tables.join("、")} · ${results}` : results;
}
