import type { CompletionKind, Run, RunEvent, RunProtocolId, RunStatus } from "@/api/types";

export const TERMINAL_RUN_STATUSES: readonly RunStatus[] = [
  "succeeded",
  "failed",
  "canceled",
];

export type RunStreamState = "idle" | "connecting" | "open" | "reconnecting" | "closed" | "error";

export interface RunProjection {
  runId: string;
  lastSeq: number;
  status: RunStatus | null;
  protocolId: RunProtocolId | null;
  cancelRequested: boolean;
  terminal: boolean;
  errorCode: string | null;
  errorMessage: string | null;
  answerMessageId: string | null;
  completionKind: CompletionKind | null;
  incompleteReason: string | null;
  answerDataFreshness: Run["answer_data_freshness"];
  historicalContextInjected: boolean;
  historicalSummaryCount: number;
  answerDraft: string | null;
  toolStatuses: Record<string, string>;
  eventLog: RunEvent[];
}

export function isTerminalRunStatus(status: RunStatus | null | undefined): boolean {
  return status !== undefined && status !== null && TERMINAL_RUN_STATUSES.includes(status);
}

export function createRunProjection(runId: string, run?: Run | null): RunProjection {
  const status = run?.status ?? null;
  return {
    runId,
    lastSeq: 0,
    status,
    protocolId: run?.protocol_id ?? null,
    cancelRequested: run?.cancel_requested_at !== null && run?.cancel_requested_at !== undefined,
    terminal: isTerminalRunStatus(status),
    errorCode: run?.error_code ?? null,
    errorMessage: run?.error_message ?? null,
    answerMessageId: null,
    completionKind: run?.completion_kind ?? null,
    incompleteReason: run?.incomplete_reason ?? null,
    answerDataFreshness: run?.answer_data_freshness ?? null,
    historicalContextInjected: run?.historical_context_injected ?? false,
    historicalSummaryCount: run?.historical_summary_count ?? 0,
    answerDraft: null,
    toolStatuses: {},
    eventLog: [],
  };
}

function payloadString(event: RunEvent, key: string): string | null {
  const value = event.payload[key];
  return typeof value === "string" ? value : null;
}

function payloadNumber(event: RunEvent, key: string): number | null {
  const value = event.payload[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function completionKind(event: RunEvent): CompletionKind | null {
  const value = payloadString(event, "completion_kind");
  return value === "completed" || value === "partial" || value === "clarification" ? value : null;
}

function answerDataFreshness(event: RunEvent): Run["answer_data_freshness"] {
  const value = payloadString(event, "answer_data_freshness");
  return value === "not_queried"
    || value === "current_schema"
    || value === "current_run_observation_only"
    || value === "current_run_evidence"
    ? value
    : null;
}

/**
 * 只投影持久化事件中的安全标量；seq 是唯一游标，重复或倒退事件不会改变状态。
 */
export function applyRunEvent(state: RunProjection, event: RunEvent): RunProjection {
  if (event.run_id !== state.runId || event.seq <= state.lastSeq) {
    return state;
  }

  const next: RunProjection = {
    ...state,
    lastSeq: event.seq,
    eventLog: [...state.eventLog, event],
    toolStatuses: { ...state.toolStatuses },
  };

  switch (event.type) {
    case "run.queued":
      next.status = "queued";
      break;
    case "run.started":
      next.status = "running";
      break;
    case "run.protocol.selected": {
      const protocolId = payloadString(event, "protocol_id");
      if (protocolId === "general-task" || protocolId === "data-analysis") {
        next.protocolId = protocolId;
      }
      break;
    }
    case "run.cancel_requested":
      next.cancelRequested = true;
      break;
    case "run.succeeded":
      next.status = "succeeded";
      next.terminal = true;
      next.answerDraft = null;
      break;
    case "run.failed":
      next.status = "failed";
      next.errorCode = payloadString(event, "error_code");
      next.errorMessage = payloadString(event, "error_message");
      next.terminal = true;
      next.answerDraft = null;
      break;
    case "run.canceled":
      next.status = "canceled";
      next.errorCode = payloadString(event, "error_code");
      next.errorMessage = payloadString(event, "error_message");
      next.terminal = true;
      next.answerDraft = null;
      break;
    case "tool.called": {
      const toolCallId = payloadString(event, "tool_call_id");
      if (toolCallId !== null) next.toolStatuses[toolCallId] = "running";
      break;
    }
    case "tool.succeeded": {
      const toolCallId = payloadString(event, "tool_call_id");
      if (toolCallId !== null) next.toolStatuses[toolCallId] = "completed";
      break;
    }
    case "tool.failed": {
      const toolCallId = payloadString(event, "tool_call_id");
      if (toolCallId !== null) next.toolStatuses[toolCallId] = "failed";
      break;
    }
    case "answer.delta": {
      const delta = payloadString(event, "delta");
      if (delta !== null && !next.terminal && next.answerMessageId === null) {
        next.answerDraft = `${next.answerDraft ?? ""}${delta}`;
      }
      break;
    }
    case "answer.ready": {
      next.answerMessageId = payloadString(event, "assistant_message_id");
      next.completionKind = completionKind(event);
      next.incompleteReason = payloadString(event, "incomplete_reason");
      next.answerDataFreshness = answerDataFreshness(event);
      next.historicalContextInjected = event.payload.historical_context_injected === true;
      next.historicalSummaryCount = payloadNumber(event, "historical_summary_count") ?? 0;
      break;
    }
    case "final_answer.validation.failed":
    case "final_answer.request.timed_out":
      next.answerDraft = null;
      break;
    default:
      break;
  }

  return next;
}
