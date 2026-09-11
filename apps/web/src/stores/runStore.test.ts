import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import type { Run, RunEvent, ToolCall } from "@/api/types";

const runApi = vi.hoisted(() => ({
  cancelRun: vi.fn(),
  getRun: vi.fn(),
  listRunArtifacts: vi.fn(),
  listRunSqlAudits: vi.fn(),
  listRunToolCalls: vi.fn(),
  listRunDatalinkConsumptions: vi.fn(),
  getTraceDag: vi.fn(),
}));
const eventApi = vi.hoisted(() => ({
  listRunEventHistory: vi.fn(),
  streamRunEvents: vi.fn(),
}));

vi.mock("@/api/runs", () => runApi);
vi.mock("@/api/runEvents", () => eventApi);

import { useRunStore } from "./runStore";

function run(
  status: Run["status"],
  completionKind: Run["completion_kind"] = null,
  incompleteReason: Run["incomplete_reason"] = null,
): Run {
  return {
    id: "run_1",
    session_id: "session_1",
    datasource_id: "datasource_1",
    datasource_deleted: false,
    user_message_id: null,
    question: "销售额是多少？",
    status,
    protocol_id: "data-analysis",
    model_profile_id: "profile_1",
    model_provider: "openai-compatible",
    model_name: "demo-model",
    schema_revision: 1,
    datalink_graph_version: "graph_1",
    run_timeout_seconds: 60,
    completion_kind: completionKind,
    incomplete_reason: incompleteReason,
    error_code: null,
    error_message: null,
    cancel_requested_at: null,
    cancel_reason: null,
    started_at: null,
    finished_at: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
  };
}

function event(seq: number, type: RunEvent["type"], payload: RunEvent["payload"] = {}): RunEvent {
  const canonicalPayload = type === "tool.called" || type === "tool.succeeded" || type === "tool.failed"
    ? { turn_no: 1, output_summary_json: "{}", ...payload }
    : type === "answer.ready"
      ? {
        assistant_message_id: "message_1",
        artifact_count: 0,
        evidence_count: 0,
        answer_format: "markdown",
        completion_kind: "completed",
        claim_audit_summary_json: "[]",
        claim_audit_truncated: false,
        answer_data_freshness: "not_queried",
        historical_context_injected: false,
        historical_summary_count: 0,
        ...payload,
      }
      : payload;
  return {
    run_id: "run_1",
    seq,
    type,
    timestamp: "2026-08-14T00:00:00Z",
    payload: canonicalPayload,
  };
}

describe("runStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    runApi.listRunToolCalls.mockResolvedValue([]);
    runApi.listRunSqlAudits.mockResolvedValue([]);
    runApi.listRunArtifacts.mockResolvedValue([]);
    runApi.listRunDatalinkConsumptions.mockResolvedValue({ run_id: "run_1", historical_status: "missing", items: [] });
    runApi.getTraceDag.mockResolvedValue({ run_id: "run_1", nodes: [], edges: [], sections: [], warnings: [] });
    eventApi.listRunEventHistory.mockResolvedValue([]);
  });

  it("starts at seq 0, closes at a terminal event, and then refreshes history", async () => {
    runApi.getRun.mockResolvedValueOnce(run("running")).mockResolvedValueOnce(run("canceled"));
    eventApi.streamRunEvents.mockImplementation(async function* () {
      yield event(1, "run.cancel_requested", { reason_code: "user_requested" });
      yield event(2, "run.canceled", { error_code: "RUN_CANCELED" });
    });
    const store = useRunStore();

    await store.begin("run_1");

    await vi.waitFor(() => expect(store.streamState).toBe("closed"));
    expect(eventApi.streamRunEvents).toHaveBeenCalledWith("run_1", {
      afterSeq: 0,
      signal: expect.any(AbortSignal),
    });
    expect(store.cancelRequested).toBe(true);
    expect(store.currentRun?.status).toBe("canceled");
    expect(runApi.getRun).toHaveBeenCalledTimes(2);
  });

  it("把实时失败事件的错误说明同步到当前 Run", async () => {
    runApi.getRun
      .mockResolvedValueOnce(run("running"))
      .mockResolvedValueOnce({
        ...run("failed"),
        error_code: "ANALYSIS_PLAN_INVALID",
        error_message: "分析计划未通过校验，请重试",
      });
    eventApi.streamRunEvents.mockImplementation(async function* () {
      yield event(1, "run.failed", {
        error_code: "ANALYSIS_PLAN_INVALID",
        error_message: "分析计划未通过校验，请重试",
      });
    });

    const store = useRunStore();
    await store.begin("run_1");

    await vi.waitFor(() => expect(store.streamState).toBe("closed"));
    expect(store.currentRun).toMatchObject({
      status: "failed",
      error_code: "ANALYSIS_PLAN_INVALID",
      error_message: "分析计划未通过校验，请重试",
    });
    expect(store.projection).toMatchObject({
      errorCode: "ANALYSIS_PLAN_INVALID",
      errorMessage: "分析计划未通过校验，请重试",
    });
  });

  it("keeps partial completion details from answer.ready until the terminal refresh", async () => {
    runApi.getRun
      .mockResolvedValueOnce(run("running"))
      .mockResolvedValueOnce(run("succeeded", "partial", "RUN_TIMEOUT"));
    eventApi.streamRunEvents.mockImplementation(async function* () {
      yield event(1, "answer.ready", {
        assistant_message_id: "message_1",
        artifact_count: 0,
        completion_kind: "partial",
        incomplete_reason: "RUN_TIMEOUT",
      });
      yield event(2, "run.succeeded");
    });
    const store = useRunStore();

    await store.begin("run_1");

    await vi.waitFor(() => expect(store.streamState).toBe("closed"));
    expect(store.currentRun).toMatchObject({
      status: "succeeded",
      completion_kind: "partial",
      incomplete_reason: "RUN_TIMEOUT",
    });
    expect(store.projection).toMatchObject({
      completionKind: "partial",
      incompleteReason: "RUN_TIMEOUT",
    });
  });

  it("在工具完成事件后刷新安全结果，而不等 Run 结束", async () => {
    const tool: ToolCall = {
      id: "tool_1",
      run_id: "run_1",
      tool_name: "run_sql_readonly",
      status: "succeeded",
      input_params: null,
      output_summary: null,
      error_code: null,
      error_message: null,
      started_at: "2026-08-14T00:00:00Z",
      finished_at: "2026-08-14T00:00:01Z",
    };
    let releaseTerminal: () => void = () => undefined;
    const terminalGate = new Promise<void>((resolve) => { releaseTerminal = resolve; });
    runApi.getRun.mockResolvedValueOnce(run("running")).mockResolvedValueOnce(run("succeeded"));
    runApi.listRunToolCalls
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([tool])
      .mockResolvedValueOnce([tool]);
    eventApi.streamRunEvents.mockImplementation(async function* () {
      yield event(1, "tool.succeeded", {
        tool_call_id: "tool_1",
        tool_name: "run_sql_readonly",
        elapsed_ms: 32,
        evidence_count: 1,
      });
      await terminalGate;
      yield event(2, "run.succeeded");
    });
    const store = useRunStore();

    await store.begin("run_1");
    await vi.waitFor(() => expect(runApi.listRunToolCalls).toHaveBeenCalledTimes(2));
    expect(store.currentRun?.status).toBe("running");
    expect(store.toolCalls).toEqual([tool]);

    releaseTerminal();
    await vi.waitFor(() => expect(store.streamState).toBe("closed"));
  });

  it("loads a terminal historical Run without creating an event stream", async () => {
    runApi.getRun.mockResolvedValue(run("succeeded", "partial", "TURN_LIMIT_REACHED"));
    const store = useRunStore();

    await store.loadHistory("run_1");

    expect(store.streamState).toBe("closed");
    expect(eventApi.streamRunEvents).not.toHaveBeenCalled();
    expect(store.currentRun).toMatchObject({
      status: "succeeded",
      completion_kind: "partial",
      incomplete_reason: "TURN_LIMIT_REACHED",
    });
    expect(store.projection).toMatchObject({
      completionKind: "partial",
      incompleteReason: "TURN_LIMIT_REACHED",
    });
  });

  it("replays all persisted event pages without opening an event stream", async () => {
    runApi.getRun.mockResolvedValue(run("succeeded"));
    eventApi.listRunEventHistory
      .mockResolvedValueOnce([
        event(1, "run.queued"),
        event(2, "tool.succeeded", {
          tool_call_id: "tool_1",
          tool_name: "run_sql_readonly",
          elapsed_ms: 32,
          evidence_count: 1,
        }),
      ])
      .mockResolvedValueOnce([
        event(3, "answer.ready", {
          assistant_message_id: "message_1",
          artifact_count: 1,
          completion_kind: "completed",
          incomplete_reason: null,
        }),
        event(4, "run.succeeded"),
      ])
      .mockResolvedValueOnce([]);
    const store = useRunStore();

    await store.loadHistory("run_1");

    expect(eventApi.listRunEventHistory).toHaveBeenNthCalledWith(1, "run_1", 0);
    expect(eventApi.listRunEventHistory).toHaveBeenNthCalledWith(2, "run_1", 2);
    expect(eventApi.listRunEventHistory).toHaveBeenNthCalledWith(3, "run_1", 4);
    expect(eventApi.streamRunEvents).not.toHaveBeenCalled();
    expect(store.projection).toMatchObject({
      lastSeq: 4,
      terminal: true,
      completionKind: "completed",
      eventLog: expect.arrayContaining([event(1, "run.queued"), event(4, "run.succeeded")]),
    });
  });

  it("DAG 读取失败时仍保留历史 Run 和线性事件，并单独暴露 DAG 错误", async () => {
    runApi.getRun.mockResolvedValue(run("succeeded"));
    eventApi.listRunEventHistory.mockResolvedValue([event(1, "run.started")]);
    runApi.getTraceDag.mockRejectedValue(new Error("Trace DAG unavailable"));
    const store = useRunStore();

    await expect(store.loadHistory("run_1")).resolves.toBeUndefined();

    expect(store.currentRun?.id).toBe("run_1");
    expect(store.projection?.eventLog).toEqual([event(1, "run.started")]);
    expect(store.traceDag).toBeNull();
    expect(store.traceDagError).toBe("Trace DAG unavailable");
    expect(store.traceDagLoading).toBe(false);
  });

  it("切换 Run 后忽略旧历史和 DAG 请求的迟到结果", async () => {
    const oldRun = { ...run("succeeded"), id: "run_old" };
    const currentRun = { ...run("succeeded"), id: "run_current" };
    let resolveOldRun: (value: Run) => void = () => undefined;
    const oldRunRequest = new Promise<Run>((resolve) => { resolveOldRun = resolve; });
    runApi.getRun.mockImplementation((runId: string) =>
      runId === "run_old" ? oldRunRequest : Promise.resolve(currentRun));
    runApi.getTraceDag.mockImplementation((runId: string) => Promise.resolve({
      run_id: runId,
      nodes: [],
      edges: [],
      sections: [],
      warnings: [],
    }));
    const store = useRunStore();

    const oldLoad = store.loadHistory("run_old");
    await store.loadHistory("run_current");
    resolveOldRun(oldRun);
    await oldLoad;

    expect(store.currentRun?.id).toBe("run_current");
    expect(store.projection?.runId).toBe("run_current");
    expect(store.traceDag?.run_id).toBe("run_current");
    expect(store.traceDagLoading).toBe(false);
  });

  it("clears the previous Run projection when the selected session has no Run", async () => {
    runApi.getRun.mockResolvedValue(run("succeeded"));
    eventApi.listRunEventHistory.mockResolvedValue([event(1, "run.succeeded")]);
    const store = useRunStore();

    await store.loadHistory("run_1");
    store.clear();

    expect(store.currentRun).toBeNull();
    expect(store.projection).toBeNull();
    expect(store.toolCalls).toEqual([]);
    expect(store.sqlAudits).toEqual([]);
    expect(store.artifacts).toEqual([]);
    expect(store.streamState).toBe("idle");
    expect(store.sessionActivityFacts).toEqual({});
  });

  it("remembers loaded Run facts so a session can show historical process cards", async () => {
    runApi.getRun.mockResolvedValue(run("succeeded"));
    eventApi.listRunEventHistory.mockResolvedValue([event(1, "run.succeeded")]);
    const store = useRunStore();

    await store.loadHistory("run_1");

    expect(store.sessionActivityFacts.run_1).toMatchObject({
      run: { id: "run_1" },
      events: [event(1, "run.succeeded")],
    });
  });

  it("hydrates other session runs without replacing the current Run", async () => {
    const current = { ...run("succeeded"), id: "run_current", user_message_id: "message_1" };
    const older = { ...run("succeeded"), id: "run_old", user_message_id: "message_2" };
    const olderEvents = [event(1, "run.succeeded")].map((item) => ({ ...item, run_id: "run_old" }));
    runApi.getRun.mockResolvedValue(current);
    eventApi.listRunEventHistory.mockImplementation((runId: string, afterSeq = 0) => {
      if (afterSeq > 0) return Promise.resolve([]);
      if (runId === "run_old") return Promise.resolve(olderEvents);
      return Promise.resolve([event(1, "run.succeeded")]);
    });
    const store = useRunStore();

    await store.loadHistory("run_current");
    await store.hydrateSessionRuns([current, older]);

    expect(store.currentRun?.id).toBe("run_current");
    expect(runApi.getTraceDag).toHaveBeenCalledTimes(1);
    expect(runApi.getTraceDag).toHaveBeenCalledWith("run_current");
    expect(store.sessionActivityFacts.run_old).toMatchObject({
      run: { id: "run_old" },
      events: olderEvents,
    });
    expect(store.sessionActivityFacts.run_current?.run?.id).toBe("run_current");
  });

  it("keeps already-hydrated runs when one sibling history request fails", async () => {
    const current = { ...run("succeeded"), id: "run_current" };
    const failed = { ...run("succeeded"), id: "run_failed" };
    runApi.getRun.mockResolvedValue(current);
    runApi.listRunToolCalls.mockImplementation((runId: string) => {
      if (runId === "run_failed") return Promise.reject(new Error("history unavailable"));
      return Promise.resolve([]);
    });
    const store = useRunStore();

    await store.loadHistory("run_current");
    await store.hydrateSessionRuns([current, failed]);

    expect(store.currentRun?.id).toBe("run_current");
    expect(store.sessionActivityFacts.run_current).toBeDefined();
    expect(store.sessionActivityFacts.run_failed).toBeUndefined();
  });

  it("ignores a stale session hydrate after the session facts are cleared", async () => {
    const older = { ...run("succeeded"), id: "run_old" };
    let resolveOlder: (value: RunEvent[]) => void = () => undefined;
    const olderRequest = new Promise<RunEvent[]>((resolve) => { resolveOlder = resolve; });
    runApi.getRun.mockResolvedValue(run("succeeded"));
    eventApi.listRunEventHistory.mockImplementation((runId: string, afterSeq = 0) => {
      if (afterSeq > 0) return Promise.resolve([]);
      if (runId === "run_old") return olderRequest;
      return Promise.resolve([event(1, "run.succeeded")]);
    });
    const store = useRunStore();

    await store.loadHistory("run_1");
    const hydrate = store.hydrateSessionRuns([run("succeeded"), older]);
    store.clearSessionActivityFacts();
    resolveOlder([event(1, "run.succeeded")]);
    await hydrate;

    expect(store.sessionActivityFacts).toEqual({});
  });
});
