import { buildApiUrl, requestJson, throwApiError } from "./client";
import type { RunEvent, RunEventPayload, RunEventType } from "./types";
import { RUN_EVENT_TYPES } from "./types";

export interface RunEventStreamOptions {
  afterSeq?: number;
  signal?: AbortSignal;
}

const RUN_EVENT_PAYLOAD_FIELDS: Readonly<Record<RunEventType, readonly string[]>> = {
  "run.queued": [],
  "run.started": [],
  "run.preparation.started": ["phase"],
  "run.preparation.completed": [
    "phase",
    "elapsed_ms",
    "status",
    "failure_code",
    "opening_model_calls",
    "opening_repair_calls",
    "validation_issue_count",
    "validation_issue_types",
    "validation_issue_reasons",
    "validation_issue_paths",
    "validation_tool_call_count",
    "validation_invalid_tool_call_count",
  ],
  "run.protocol.selected": ["protocol_id", "selection_mode", "planning_mode"],
  "analysis.clarification.requested": ["reason", "requirement_count"],
  "analysis.requirement.blocked": ["requirement_id", "reason_code"],
  "analysis.discovery.observed": [
    "tool_call_id",
    "tool_name",
    "turn_no",
    "audit_log_id",
    "artifact_id",
    "column_count",
    "row_count",
    "rows_truncated",
  ],
  "agent.turn.started": ["turn_no"],
  "agent.turn.completed": [
    "turn_no",
    "elapsed_ms",
    "status",
    "action_kind",
    "tool_names",
    "tool_call_count",
    "failure_code",
    "reason_code",
    "reasoning",
    "assistant_output",
    "context_retry_count",
    "context_compaction_count",
    "working_set_count",
    "working_set_compacted",
    "model_input_chars",
    "model_input_tokens",
    "estimated_total_tokens",
    "context_window_tokens",
    "input_budget_tokens",
    "remaining_tokens",
    "estimate_source",
    "working_set_value_count",
  ],
  "final_answer.request.started": ["attempt", "mode"],
  "final_answer.response.received": ["attempt", "mode", "elapsed_ms"],
  "final_answer.validation.failed": [
    "attempt",
    "mode",
    "elapsed_ms",
    "failure_code",
    "validation_stage",
  ],
  "final_answer.request.timed_out": ["attempt", "mode", "elapsed_ms", "failure_code"],
  "tool.called": ["tool_call_id", "tool_name", "turn_no"],
  "tool.succeeded": ["tool_call_id", "tool_name", "turn_no", "elapsed_ms", "evidence_count", "output_summary_json"],
  "tool.failed": [
    "tool_call_id",
    "tool_name",
    "turn_no",
    "elapsed_ms",
    "output_summary_json",
    "error_code",
    "reason_code",
    "error_message",
    "hint",
    "retryable",
    "subject",
    "line",
    "column",
  ],
  "artifact.created": ["artifact_id", "artifact_type"],
  "answer.delta": ["delta"],
  "answer.ready": [
    "assistant_message_id",
    "artifact_count",
    "evidence_count",
    "answer_format",
    "completion_kind",
    "incomplete_reason",
    "claim_audit_summary_json",
    "claim_audit_truncated",
    "answer_data_freshness",
    "historical_context_injected",
    "historical_summary_count",
  ],
  "run.cancel_requested": ["reason_code"],
  "run.succeeded": [],
  "run.failed": ["error_code", "error_message"],
  "run.canceled": ["error_code", "error_message"],
};

const PREPARATION_PHASES = ["run_opening", "semantic_context", "analysis_plan"] as const;
const PREPARATION_STATUSES = ["completed", "timed_out", "cancelled", "failed"] as const;
const FINAL_ANSWER_MODES = ["markdown", "json_schema", "json_object", "submit_answer"] as const;
const FINAL_ANSWER_FAILURE_CODES = [
  "MODEL_OUTPUT_INVALID",
  "FINAL_ANSWER_TIMEOUT",
  "FINAL_ANSWER_FACT_MISMATCH",
  "CONTEXT_BUDGET_EXHAUSTED",
] as const;
const FINAL_ANSWER_VALIDATION_STAGES = ["dto", "markdown", "budget"] as const;
const AGENT_TURN_STATUSES = ["completed", "failed", "cancelled"] as const;
const AGENT_TURN_ACTION_KINDS = ["tool_call", "respond", "model_error"] as const;
const AGENT_TURN_TOOL_NAMES = [
  "run_sql_readonly",
  "run_python",
  "explore_datalink",
  "commit_analysis_claims",
] as const;
const MAX_AGENT_TURN_TEXT_LENGTH = 16_000;

const REQUIRED_PAYLOAD_FIELDS: Readonly<Record<RunEventType, readonly string[]>> = {
  "run.queued": [],
  "run.started": [],
  "run.preparation.started": ["phase"],
  "run.preparation.completed": ["phase", "elapsed_ms", "status"],
  "run.protocol.selected": ["protocol_id", "selection_mode"],
  "analysis.clarification.requested": ["reason", "requirement_count"],
  "analysis.requirement.blocked": ["requirement_id", "reason_code"],
  "analysis.discovery.observed": ["tool_call_id", "tool_name", "turn_no", "audit_log_id", "artifact_id", "column_count", "row_count", "rows_truncated"],
  "agent.turn.started": ["turn_no"],
  "agent.turn.completed": ["turn_no", "elapsed_ms", "status", "action_kind", "tool_names", "tool_call_count"],
  "final_answer.request.started": ["attempt", "mode"],
  "final_answer.response.received": ["attempt", "mode", "elapsed_ms"],
  "final_answer.validation.failed": ["attempt", "mode", "elapsed_ms", "failure_code", "validation_stage"],
  "final_answer.request.timed_out": ["attempt", "mode", "elapsed_ms", "failure_code"],
  "tool.called": ["tool_call_id", "tool_name", "turn_no"],
  "tool.succeeded": ["tool_call_id", "tool_name", "turn_no", "elapsed_ms", "evidence_count", "output_summary_json"],
  "tool.failed": ["tool_call_id", "tool_name", "turn_no", "elapsed_ms", "output_summary_json", "error_code"],
  "artifact.created": ["artifact_id", "artifact_type"],
  "answer.delta": ["delta"],
  "answer.ready": ["assistant_message_id", "artifact_count", "evidence_count", "answer_format", "completion_kind", "claim_audit_summary_json", "claim_audit_truncated", "answer_data_freshness", "historical_context_injected", "historical_summary_count"],
  "run.cancel_requested": ["reason_code"],
  "run.succeeded": [],
  "run.failed": ["error_code", "error_message"],
  "run.canceled": ["error_code", "error_message"],
};

const FINAL_ANSWER_EVENTS = [
  "final_answer.request.started",
  "final_answer.response.received",
  "final_answer.validation.failed",
  "final_answer.request.timed_out",
] as const;

function isPreparationPhase(value: unknown): value is (typeof PREPARATION_PHASES)[number] {
  return typeof value === "string" && PREPARATION_PHASES.some((phase) => phase === value);
}

function isPreparationStatus(value: unknown): value is (typeof PREPARATION_STATUSES)[number] {
  return typeof value === "string" && PREPARATION_STATUSES.some((status) => status === value);
}

function isFinalAnswerEvent(value: RunEventType): value is (typeof FINAL_ANSWER_EVENTS)[number] {
  return FINAL_ANSWER_EVENTS.some((eventType) => eventType === value);
}

function isFinalAnswerMode(value: unknown): value is (typeof FINAL_ANSWER_MODES)[number] {
  return typeof value === "string" && FINAL_ANSWER_MODES.some((mode) => mode === value);
}

function isFinalAnswerFailureCode(value: unknown): value is (typeof FINAL_ANSWER_FAILURE_CODES)[number] {
  return typeof value === "string" && FINAL_ANSWER_FAILURE_CODES.some((code) => code === value);
}

function isFinalAnswerValidationStage(value: unknown): value is (typeof FINAL_ANSWER_VALIDATION_STAGES)[number] {
  return typeof value === "string" && FINAL_ANSWER_VALIDATION_STAGES.some((stage) => stage === value);
}

function isAgentTurnStatus(value: unknown): value is (typeof AGENT_TURN_STATUSES)[number] {
  return typeof value === "string" && AGENT_TURN_STATUSES.some((status) => status === value);
}

function isAgentTurnActionKind(value: unknown): value is (typeof AGENT_TURN_ACTION_KINDS)[number] {
  return typeof value === "string" && AGENT_TURN_ACTION_KINDS.some((kind) => kind === value);
}

function isAgentTurnToolNames(value: unknown): value is string {
  if (typeof value !== "string") return false;
  if (value === "") return true;
  const names = value.split(",");
  return names.length <= 8 && new Set(names).size === names.length && names.every(
    (name) => AGENT_TURN_TOOL_NAMES.some((known) => known === name),
  );
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isPositiveTurnNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1 && value <= 10_000;
}

function isRunProtocolId(value: unknown): value is "general-task" | "data-analysis" {
  return value === "general-task" || value === "data-analysis";
}

function isPlanningMode(value: unknown): boolean {
  return value === "ready" || value === "discovery" ||
    value === "needs_semantic_context" || value === "clarification";
}

export function listRunEventHistory(runId: string, afterSeq = 0): Promise<RunEvent[]> {
  return requestJson<RunEvent[]>(
    `/runs/${encodeURIComponent(runId)}/events/history?after_seq=${afterSeq}`,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isScalar(value: unknown): value is string | number | boolean | null {
  return value === null || typeof value === "string" || typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value));
}

function isRunEventType(value: unknown): value is RunEventType {
  return typeof value === "string" && RUN_EVENT_TYPES.some((type) => type === value);
}

function parsePayload(value: unknown, eventType: RunEventType): RunEventPayload | null {
  if (!isRecord(value) || Array.isArray(value)) {
    return null;
  }
  const payload: RunEventPayload = {};
  const allowedFields = RUN_EVENT_PAYLOAD_FIELDS[eventType];
  for (const [key, item] of Object.entries(value)) {
    if (!allowedFields.includes(key)) {
      return null;
    }
    if (!isScalar(item)) {
      return null;
    }
    if (
      key === "phase" &&
      (eventType === "run.preparation.started" || eventType === "run.preparation.completed") &&
      !isPreparationPhase(item)
    ) {
      return null;
    }
    if (
      key === "status" &&
      eventType === "run.preparation.completed" &&
      !isPreparationStatus(item)
    ) {
      return null;
    }
    if (
      eventType === "run.preparation.completed" &&
      (key === "opening_model_calls" || key === "opening_repair_calls") &&
      !isNonNegativeSafeInteger(item)
    ) {
      return null;
    }
    if (
      eventType === "run.preparation.completed" &&
      (key === "validation_tool_call_count" || key === "validation_invalid_tool_call_count") &&
      !isNonNegativeSafeInteger(item)
    ) {
      return null;
    }
    if (eventType === "run.protocol.selected") {
      if (key === "protocol_id" && !isRunProtocolId(item)) return null;
      if (key === "selection_mode" && item !== "opening" && item !== "replayed") return null;
      if (key === "planning_mode" && !isPlanningMode(item)) return null;
    }
    if (isFinalAnswerEvent(eventType)) {
      if (key === "attempt" && (!isNonNegativeSafeInteger(item) || item < 1)) {
        return null;
      }
      if (key === "mode" && !isFinalAnswerMode(item)) {
        return null;
      }
      if (key === "elapsed_ms" && !isNonNegativeSafeInteger(item)) {
        return null;
      }
      if (key === "failure_code" && !isFinalAnswerFailureCode(item)) {
        return null;
      }
      if (key === "validation_stage" &&
        (eventType !== "final_answer.validation.failed" || !isFinalAnswerValidationStage(item))) {
        return null;
      }
    }
    if (eventType === "agent.turn.completed") {
      if (key === "turn_no" && (!isNonNegativeSafeInteger(item) || item < 1)) return null;
      if (key === "elapsed_ms" && !isNonNegativeSafeInteger(item)) return null;
      if (key === "status" && !isAgentTurnStatus(item)) return null;
      if (key === "action_kind" && !isAgentTurnActionKind(item)) return null;
      if (key === "tool_names" && !isAgentTurnToolNames(item)) return null;
      if (key === "tool_call_count" && (!isNonNegativeSafeInteger(item) || item > 8)) return null;
      if (
        [
          "context_retry_count",
          "context_compaction_count",
          "working_set_count",
          "model_input_chars",
          "model_input_tokens",
          "estimated_total_tokens",
          "context_window_tokens",
          "input_budget_tokens",
          "remaining_tokens",
          "working_set_value_count",
        ].includes(key) && !isNonNegativeSafeInteger(item)
      ) return null;
      if (key === "context_retry_count" && (!isNonNegativeSafeInteger(item) || item > 1)) {
        return null;
      }
      if (key === "context_compaction_count" && (!isNonNegativeSafeInteger(item) || item > 64)) {
        return null;
      }
      if (key === "working_set_count" && (!isNonNegativeSafeInteger(item) || item > 64)) {
        return null;
      }
      if (key === "working_set_compacted" && typeof item !== "boolean") return null;
      if (key === "estimate_source" &&
        (typeof item !== "string" || !/^[A-Za-z0-9_.:-]{1,80}$/.test(item))) return null;
      if (
        (key === "reasoning" || key === "assistant_output") &&
        (typeof item !== "string" || item.length === 0 || item.length > MAX_AGENT_TURN_TEXT_LENGTH)
      ) return null;
    }
    if (
      (eventType === "tool.called" || eventType === "tool.succeeded" ||
        eventType === "tool.failed" || eventType === "analysis.discovery.observed") &&
      key === "turn_no" &&
      !isPositiveTurnNumber(item)
    ) {
      return null;
    }
    if (
      eventType === "tool.failed" &&
      key === "elapsed_ms" &&
      !isNonNegativeSafeInteger(item)
    ) {
      return null;
    }
    payload[key] = item;
  }
  for (const key of REQUIRED_PAYLOAD_FIELDS[eventType]) {
    if (!Object.prototype.hasOwnProperty.call(payload, key)) return null;
  }
  if (eventType === "run.protocol.selected") {
    if (!isRunProtocolId(payload.protocol_id) || payload.selection_mode !== "opening") return null;
    if (payload.planning_mode !== undefined && !isPlanningMode(payload.planning_mode)) return null;
  }
  if (eventType === "run.preparation.completed") {
    const openingCalls = payload.opening_model_calls;
    const repairCalls = payload.opening_repair_calls;
    if ((openingCalls === undefined) !== (repairCalls === undefined)) return null;
    if (openingCalls !== undefined || repairCalls !== undefined) {
      if (
        payload.phase !== "run_opening" ||
        !isNonNegativeSafeInteger(openingCalls) ||
        !isNonNegativeSafeInteger(repairCalls)
      ) {
        return null;
      }
      if (openingCalls < 1 || openingCalls > 2 || repairCalls > 1 || repairCalls > openingCalls - 1) {
        return null;
      }
    }
  }
  if (eventType === "analysis.discovery.observed") {
    if (
      payload.tool_name !== "run_sql_readonly" ||
      typeof payload.tool_call_id !== "string" ||
      typeof payload.audit_log_id !== "string" ||
      typeof payload.artifact_id !== "string" ||
      !isNonNegativeSafeInteger(payload.column_count) ||
      !isNonNegativeSafeInteger(payload.row_count) ||
      typeof payload.rows_truncated !== "boolean"
    ) return null;
  }
  return payload;
}

/** 所有外部 SSE JSON 都从 unknown 收窄到安全事件，页面不需要自行断言。 */
export function parseRunEvent(value: unknown, eventName?: string): RunEvent | null {
  if (!isRecord(value) || typeof value.run_id !== "string" || typeof value.seq !== "number" ||
    !Number.isSafeInteger(value.seq) || value.seq < 1 || typeof value.timestamp !== "string" ||
    !isRunEventType(value.type)) {
    return null;
  }
  if (eventName !== undefined && eventName !== "" && eventName !== value.type) {
    return null;
  }
  const payload = parsePayload(value.payload, value.type);
  if (payload === null) {
    return null;
  }
  return {
    run_id: value.run_id,
    seq: value.seq,
    type: value.type,
    timestamp: value.timestamp,
    payload,
  };
}

/** 将一帧 SSE（心跳或空帧返回 null）转换成安全事件。 */
export function parseSseFrame(frame: string): RunEvent | null {
  let eventName = "";
  const dataLines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) {
      continue;
    }
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") {
      eventName = value;
    } else if (field === "data") {
      dataLines.push(value);
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(dataLines.join("\n"));
    return parseRunEvent(parsed, eventName);
  } catch {
    return null;
  }
}

/** 按后端 after_seq 连接 SSE；终态事件到达后生成器自然结束。 */
export async function* streamRunEvents(
  runId: string,
  options: RunEventStreamOptions = {},
): AsyncGenerator<RunEvent, void, unknown> {
  const query = new URLSearchParams();
  query.set("after_seq", String(options.afterSeq ?? 0));
  const response = await fetch(
    `${buildApiUrl(`/runs/${encodeURIComponent(runId)}/events`)}?${query.toString()}`,
    {
      headers: { Accept: "text/event-stream" },
      signal: options.signal,
    },
  );
  if (!response.ok) {
    await throwApiError(response);
    return;
  }
  if (response.body === null) {
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const event = parseSseFrame(frame);
        if (event !== null) {
          yield event;
          if (event.type === "run.succeeded" || event.type === "run.failed" || event.type === "run.canceled") {
            return;
          }
        }
      }
      if (chunk.done) {
        const event = parseSseFrame(buffer);
        if (event !== null) {
          yield event;
        }
        return;
      }
    }
  } finally {
    await reader.cancel();
  }
}
