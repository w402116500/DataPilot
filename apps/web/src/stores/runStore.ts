import { computed, ref, shallowRef } from "vue";
import { defineStore } from "pinia";

import { cancelRun, getRun, getTraceDag, listRunArtifacts, listRunDatalinkConsumptions, listRunSqlAudits, listRunToolCalls } from "@/api/runs";
import { listRunEventHistory, streamRunEvents } from "@/api/runEvents";
import type { DataLinkConsumptionList, Run, RunArtifact, RunEvent, SqlAudit, ToolCall, TraceDag } from "@/api/types";

import type { RunActivityFacts } from "@/lib/runActivity";

import {
  applyRunEvent,
  createRunProjection,
  isTerminalRunStatus,
  type RunProjection,
  type RunStreamState,
} from "./runProjection";

const RECONNECT_DELAY_MS = 1_000;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "运行状态读取失败";
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export const useRunStore = defineStore("run", () => {
  const currentRun = ref<Run | null>(null);
  const projection = ref<RunProjection | null>(null);
  const toolCalls = ref<ToolCall[]>([]);
  const sqlAudits = ref<SqlAudit[]>([]);
  const artifacts = ref<RunArtifact[]>([]);
  const datalinkConsumptions = ref<DataLinkConsumptionList | null>(null);
  const traceDag = ref<TraceDag | null>(null);
  const traceDagLoading = ref(false);
  const traceDagError = ref<string | null>(null);
  const streamState = ref<RunStreamState>("idle");
  const error = ref<string | null>(null);
  const sessionActivityFacts = ref<Record<string, RunActivityFacts>>({});
  const controller = shallowRef<AbortController | null>(null);
  let connectionToken = 0;
  let connectionTask: Promise<void> | null = null;
  let factRefreshToken = 0;
  let historyRefreshToken = 0;
  let traceDagRefreshToken = 0;
  let sessionFactsToken = 0;

  const isActive = computed(
    () => currentRun.value !== null && !isTerminalRunStatus(currentRun.value.status),
  );
  const cancelRequested = computed(() => projection.value?.cancelRequested ?? false);

  function reset(runId: string, run?: Run | null): void {
    factRefreshToken += 1;
    historyRefreshToken += 1;
    traceDagRefreshToken += 1;
    currentRun.value = run ?? null;
    projection.value = createRunProjection(runId, run);
    toolCalls.value = [];
    sqlAudits.value = [];
    artifacts.value = [];
    datalinkConsumptions.value = null;
    traceDag.value = null;
    traceDagLoading.value = false;
    traceDagError.value = null;
    error.value = null;
    streamState.value = isTerminalRunStatus(run?.status) ? "closed" : "idle";
  }

  function stopStream(): void {
    connectionToken += 1;
    controller.value?.abort();
    controller.value = null;
    connectionTask = null;
    if (projection.value?.terminal) streamState.value = "closed";
    else if (streamState.value !== "idle") streamState.value = "idle";
  }

  function clearSessionActivityFacts(): void {
    sessionFactsToken += 1;
    sessionActivityFacts.value = {};
  }

  function rememberActivityFacts(runId: string): void {
    const run = currentRun.value;
    if (run === null || run.id !== runId) return;
    sessionActivityFacts.value = {
      ...sessionActivityFacts.value,
      [runId]: {
        run,
        events: projection.value?.eventLog ?? [],
        toolCalls: toolCalls.value,
        sqlAudits: sqlAudits.value,
        artifacts: artifacts.value,
      },
    };
  }

  async function fetchActivityFacts(run: Run): Promise<RunActivityFacts> {
    const [runToolCalls, runAudits, runArtifacts, runEvents] = await Promise.all([
      listRunToolCalls(run.id),
      listRunSqlAudits(run.id),
      listRunArtifacts(run.id),
      loadEventHistory(run.id),
    ]);
    return {
      run,
      events: runEvents,
      toolCalls: runToolCalls,
      sqlAudits: runAudits,
      artifacts: runArtifacts,
    };
  }

  async function hydrateSessionRuns(sessionRuns: readonly Run[]): Promise<void> {
    const token = ++sessionFactsToken;
    const pending = sessionRuns.filter((run) => sessionActivityFacts.value[run.id] === undefined);
    if (pending.length === 0) return;
    const settled = await Promise.allSettled(
      pending.map(async (run) => [run.id, await fetchActivityFacts(run)] as const),
    );
    if (token !== sessionFactsToken) return;
    const next = { ...sessionActivityFacts.value };
    for (const result of settled) {
      if (result.status === "fulfilled") next[result.value[0]] = result.value[1];
    }
    sessionActivityFacts.value = next;
  }

  function clear(): void {
    stopStream();
    factRefreshToken += 1;
    historyRefreshToken += 1;
    traceDagRefreshToken += 1;
    currentRun.value = null;
    projection.value = null;
    toolCalls.value = [];
    sqlAudits.value = [];
    artifacts.value = [];
    datalinkConsumptions.value = null;
    traceDag.value = null;
    traceDagLoading.value = false;
    traceDagError.value = null;
    error.value = null;
    clearSessionActivityFacts();
    streamState.value = "idle";
  }

  function applyEvent(event: RunEvent): void {
    if (projection.value === null) return;
    const next = applyRunEvent(projection.value, event);
    if (next === projection.value) return;
    projection.value = next;
    if (currentRun.value !== null) {
      currentRun.value = {
        ...currentRun.value,
        status: next.status ?? currentRun.value.status,
        protocol_id: next.protocolId ?? currentRun.value.protocol_id,
        cancel_requested_at: next.cancelRequested
          ? currentRun.value.cancel_requested_at ?? new Date().toISOString()
          : currentRun.value.cancel_requested_at,
        error_code: next.errorCode ?? currentRun.value.error_code,
        error_message: next.errorMessage ?? currentRun.value.error_message,
        completion_kind: next.completionKind ?? currentRun.value.completion_kind,
        incomplete_reason: next.incompleteReason ?? currentRun.value.incomplete_reason,
        answer_data_freshness:
          event.type === "answer.ready"
            ? next.answerDataFreshness
            : currentRun.value.answer_data_freshness,
        historical_context_injected:
          event.type === "answer.ready"
            ? next.historicalContextInjected
            : currentRun.value.historical_context_injected,
        historical_summary_count:
          event.type === "answer.ready"
            ? next.historicalSummaryCount
            : currentRun.value.historical_summary_count,
      };
    }
  }

  function clearAnswerDraft(): void {
    if (projection.value === null || projection.value.answerDraft === null) return;
    projection.value.answerDraft = null;
  }

  async function loadEventHistory(runId: string): Promise<RunEvent[]> {
    const events: RunEvent[] = [];
    let afterSeq = 0;
    while (true) {
      const page = await listRunEventHistory(runId, afterSeq);
      if (page.length === 0) return events;
      events.push(...page);
      const lastEvent = page.at(-1);
      if (lastEvent === undefined || lastEvent.seq <= afterSeq) return events;
      afterSeq = lastEvent.seq;
    }
  }

  async function refreshHistory(runId: string): Promise<void> {
    factRefreshToken += 1;
    const historyToken = ++historyRefreshToken;
    const dagToken = ++traceDagRefreshToken;
    traceDagLoading.value = true;
    traceDagError.value = null;
    const dagRequest = getTraceDag(runId).then(
      (dag) => ({ dag, error: null as string | null }),
      (caught: unknown) => ({ dag: null, error: errorMessage(caught) }),
    );
    const [run, runToolCalls, runAudits, runArtifacts, runEvents, dagResult, consumptions] = await Promise.all([
      getRun(runId),
      listRunToolCalls(runId),
      listRunSqlAudits(runId),
      listRunArtifacts(runId),
      loadEventHistory(runId),
      dagRequest,
      listRunDatalinkConsumptions(runId),
    ]);
    if (historyToken !== historyRefreshToken || projection.value?.runId !== runId) return;
    currentRun.value = run;
    if (projection.value === null || projection.value.runId !== runId) {
      projection.value = createRunProjection(runId, run);
    } else {
      projection.value = {
        ...projection.value,
        status: run.status,
        protocolId: run.protocol_id,
        cancelRequested: projection.value.cancelRequested || run.cancel_requested_at !== null,
        terminal: isTerminalRunStatus(run.status),
        errorCode: run.error_code,
        errorMessage: run.error_message,
        completionKind: run.completion_kind,
        incompleteReason: run.incomplete_reason,
        answerDataFreshness: run.answer_data_freshness ?? null,
        historicalContextInjected: run.historical_context_injected ?? false,
        historicalSummaryCount: run.historical_summary_count ?? 0,
      };
    }
    toolCalls.value = runToolCalls;
    sqlAudits.value = runAudits;
    artifacts.value = runArtifacts;
    datalinkConsumptions.value = consumptions;
    if (dagToken === traceDagRefreshToken) {
      traceDag.value = dagResult.dag;
      traceDagError.value = dagResult.error;
      traceDagLoading.value = false;
    }
    for (const event of runEvents) applyEvent(event);
    if (isTerminalRunStatus(run.status)) streamState.value = "closed";
    rememberActivityFacts(runId);
  }

  async function refreshTraceDag(runId: string): Promise<void> {
    const token = ++traceDagRefreshToken;
    traceDagLoading.value = true;
    traceDagError.value = null;
    try {
      const dag = await getTraceDag(runId);
      if (token !== traceDagRefreshToken || projection.value?.runId !== runId) return;
      traceDag.value = dag;
    } catch (caught) {
      if (token !== traceDagRefreshToken || projection.value?.runId !== runId) return;
      traceDagError.value = errorMessage(caught);
    } finally {
      if (token === traceDagRefreshToken && projection.value?.runId === runId) {
        traceDagLoading.value = false;
      }
    }
  }

  async function refreshRunFacts(runId: string): Promise<void> {
    const token = ++factRefreshToken;
    const [runToolCalls, runAudits, runArtifacts, consumptions] = await Promise.all([
      listRunToolCalls(runId),
      listRunSqlAudits(runId),
      listRunArtifacts(runId),
      listRunDatalinkConsumptions(runId),
    ]);
    if (token !== factRefreshToken || projection.value?.runId !== runId) return;
    toolCalls.value = runToolCalls;
    sqlAudits.value = runAudits;
    artifacts.value = runArtifacts;
    datalinkConsumptions.value = consumptions;
    await refreshTraceDag(runId);
  }

  function refreshFactsAfter(event: RunEvent): void {
    if (
      event.type !== "tool.succeeded" &&
      event.type !== "tool.failed" &&
      event.type !== "artifact.created"
    ) {
      return;
    }
    void refreshRunFacts(event.run_id).catch(() => {
      // 过程事件仍会保留；下一次事件或终态完整刷新会重新对账安全投影。
    });
  }

  async function consumeStream(runId: string, token: number): Promise<void> {
    while (token === connectionToken && projection.value?.runId === runId && !projection.value.terminal) {
      const nextController = new AbortController();
      controller.value = nextController;
      streamState.value = streamState.value === "reconnecting" ? "reconnecting" : "connecting";
      try {
        const afterSeq = projection.value.lastSeq;
        for await (const event of streamRunEvents(runId, {
          afterSeq,
          signal: nextController.signal,
        })) {
          if (token !== connectionToken) return;
          streamState.value = "open";
          applyEvent(event);
          refreshFactsAfter(event);
          if (projection.value?.terminal) break;
        }
        if (projection.value?.terminal) {
          streamState.value = "closed";
          await refreshHistory(runId);
          return;
        }
      } catch (caught) {
        if (token !== connectionToken || isAbortError(caught)) return;
        error.value = errorMessage(caught);
      } finally {
        if (controller.value === nextController) controller.value = null;
      }
      if (token !== connectionToken || projection.value?.terminal) return;
      streamState.value = "reconnecting";
      await new Promise<void>((resolve) => window.setTimeout(resolve, RECONNECT_DELAY_MS));
    }
  }

  function startStream(runId: string): void {
    if (connectionTask !== null && projection.value?.runId === runId) return;
    stopStream();
    const token = connectionToken;
    streamState.value = "connecting";
    const task = consumeStream(runId, token);
    connectionTask = task.finally(() => {
      if (token === connectionToken) connectionTask = null;
    });
  }

  async function begin(runId: string): Promise<void> {
    stopStream();
    reset(runId);
    try {
      await refreshHistory(runId);
      if (currentRun.value !== null && !isTerminalRunStatus(currentRun.value.status)) {
        startStream(runId);
      }
    } catch (caught) {
      error.value = errorMessage(caught);
      streamState.value = "error";
    }
  }

  async function loadHistory(runId: string): Promise<void> {
    stopStream();
    reset(runId);
    try {
      await refreshHistory(runId);
    } catch (caught) {
      error.value = errorMessage(caught);
      streamState.value = "error";
    }
  }

  async function requestCancel(runId: string, reason = "user_requested"): Promise<void> {
    try {
      const result = await cancelRun(runId, { reason });
      if (currentRun.value?.id === runId) {
        currentRun.value = {
          ...currentRun.value,
          status: result.status,
          cancel_requested_at: result.cancel_requested_at,
        };
      }
      if (projection.value?.runId === runId) {
        projection.value = { ...projection.value, cancelRequested: result.cancel_requested_at !== null };
      }
    } catch (caught) {
      error.value = errorMessage(caught);
      throw caught;
    }
  }

  return {
    currentRun,
    projection,
    toolCalls,
    sqlAudits,
    artifacts,
    datalinkConsumptions,
    streamState,
    error,
    traceDag,
    traceDagLoading,
    traceDagError,
    isActive,
    cancelRequested,
    applyEvent,
    clearAnswerDraft,
    begin,
    loadHistory,
    refreshHistory,
    refreshTraceDag,
    requestCancel,
    startStream,
    stopStream,
    clear,
    sessionActivityFacts,
    clearSessionActivityFacts,
    hydrateSessionRuns,
  };
});
