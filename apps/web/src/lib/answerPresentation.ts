import type { Message, Run, RunArtifact } from "@/api/types";

import { isSafeChartMime } from "./artifactContent";

const INTERNAL_EVIDENCE_ID = /\b(audit|artifact)_(?:\[[^\]\r\n]+\]_)?(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{8,}\b/giu;

/** 正式答案保留事实文字，但不把不可操作的内部证据 ID 暴露给用户。 */
export function presentAssistantAnswer(markdown: string): string {
  return markdown.replace(INTERNAL_EVIDENCE_ID, (_match, kind: string) =>
    kind === "audit" ? "本次审计记录" : "本次分析产物",
  );
}

/** 正式答案内嵌本 Run 已登记的图表产物，不解析 Markdown 里的 ASCII 图。 */
export function chartArtifactsForAnswer(
  message: Pick<Message, "role" | "run_id">,
  artifacts: readonly RunArtifact[],
): RunArtifact[] {
  if (message.role !== "assistant" || message.run_id === null) return [];
  const runId = message.run_id;
  return artifacts.filter(
    (artifact) =>
      artifact.type === "chart"
      && artifact.run_id === runId
      && artifact.inline_previewable
      && isSafeChartMime(artifact.mime_type),
  );
}

/** 用服务端来源事实解释答案，前端不重新推断查询或证据状态。 */
export function answerProvenanceLabel(run: Run | null): string | null {
  if (run?.status !== "succeeded") return null;
  if (run.answer_data_freshness === "not_queried") {
    return run.historical_context_injected
      ? (run.historical_summary_count ?? 0) > 0
        ? "参考了本会话历史回答摘要；本次未重新查询当前数据。"
        : "参考了本会话历史上下文；本次未重新查询当前数据。"
      : "本次未查询当前数据。";
  }
  if (run.answer_data_freshness === "current_schema") {
    return "结论仅依据本次 Run 固定的 Schema。";
  }
  if (run.answer_data_freshness === "current_run_observation_only") {
    return "本次 Run 已完成观察，但尚未形成可引用的已验证结论。";
  }
  if (run.answer_data_freshness === "current_run_evidence") {
    return "历史内容仅用于理解；结论来自本次 Run 的已验证数据。";
  }
  return null;
}
