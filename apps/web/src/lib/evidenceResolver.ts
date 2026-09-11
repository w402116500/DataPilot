import type { Run, RunArtifact, SqlAudit, ToolCall } from "@/api/types";

import { toolLabel } from "./runActivity";

interface EvidenceBase {
  ref: string;
  label: string;
}

export type ResolvedEvidence =
  | (EvidenceBase & { kind: "schema"; detailId: "schema" })
  | (EvidenceBase & { kind: "tool"; detailId: string; tool: ToolCall })
  | (EvidenceBase & { kind: "audit"; detailId: string; audit: SqlAudit })
  | (EvidenceBase & { kind: "artifact"; detailId: string; artifact: RunArtifact })
  | (EvidenceBase & { kind: "missing" | "ambiguous"; detailId: null; reason: string });

interface EvidenceCatalogInput {
  run: Run;
  toolCalls: readonly ToolCall[];
  sqlAudits: readonly SqlAudit[];
  artifacts: readonly RunArtifact[];
}

export interface EvidenceCatalog {
  resolve(ref: string): ResolvedEvidence;
}

export interface ResolvedAnswerEvidence {
  refs: string[];
  evidences: ResolvedEvidence[];
}

export function formatSqlAuditLabel(attemptNo: number): string {
  return `第 ${attemptNo + 1} 次 SQL 查询`;
}

function unavailable(ref: string, kind: "missing" | "ambiguous"): ResolvedEvidence {
  return {
    kind,
    ref,
    label: kind === "ambiguous" ? "证据引用冲突" : "证据暂不可用",
    detailId: null,
    reason: kind === "ambiguous"
      ? "同一个引用命中了多类运行记录，不能自动选择。"
      : "当前 Run 中没有找到这条证据。",
  };
}

/** 只解析当前 Run 的事实，避免历史消息误选到另一个 Run 的同名记录。 */
export function createEvidenceCatalog({
  run,
  toolCalls,
  sqlAudits,
  artifacts,
}: EvidenceCatalogInput): EvidenceCatalog {
  const candidates = new Map<string, ResolvedEvidence[]>();

  function add(ref: string, evidence: ResolvedEvidence): void {
    const current = candidates.get(ref) ?? [];
    current.push(evidence);
    candidates.set(ref, current);
  }

  add("schema", {
    kind: "schema",
    ref: "schema",
    label: run.schema_revision === null ? "数据结构" : `数据结构 r${run.schema_revision}`,
    detailId: "schema",
  });

  for (const tool of toolCalls) {
    if (tool.run_id !== run.id) continue;
    add(tool.id, {
      kind: "tool",
      ref: tool.id,
      label: toolLabel(tool.tool_name),
      detailId: `tool:${tool.id}`,
      tool,
    });
  }

  for (const audit of sqlAudits) {
    if (audit.run_id !== run.id) continue;
    add(audit.id, {
      kind: "audit",
      ref: audit.id,
      label: formatSqlAuditLabel(audit.attempt_no),
      detailId: `audit:${audit.id}`,
      audit,
    });
  }

  for (const artifact of artifacts) {
    if (artifact.run_id !== run.id) continue;
    add(artifact.id, {
      kind: "artifact",
      ref: artifact.id,
      label: artifact.title,
      detailId: `artifact:${artifact.id}`,
      artifact,
    });
  }

  return {
    resolve(ref: string): ResolvedEvidence {
      const matches = candidates.get(ref) ?? [];
      if (matches.length === 0) return unavailable(ref, "missing");
      if (matches.length > 1) return unavailable(ref, "ambiguous");
      return matches[0] ?? unavailable(ref, "missing");
    },
  };
}

export function isEvidenceActionable(
  evidence: ResolvedEvidence,
): evidence is Exclude<ResolvedEvidence, { kind: "missing" | "ambiguous" }> {
  return evidence.detailId !== null;
}

export function resolveAnswerEvidence(
  refs: readonly string[],
  catalog: EvidenceCatalog,
  options: { requireActionableEvidence?: boolean } = {},
): ResolvedAnswerEvidence | null {
  const uniqueRefs = [...new Set(refs)].filter((ref) => ref.trim().length > 0);
  if (uniqueRefs.length === 0) return null;
  const evidences = uniqueRefs.map((ref) => catalog.resolve(ref));
  if (options.requireActionableEvidence && !evidences.some(isEvidenceActionable)) return null;
  return { refs: uniqueRefs, evidences };
}
