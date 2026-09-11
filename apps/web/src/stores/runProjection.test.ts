import { describe, expect, it } from "vitest";

import type { RunEvent } from "@/api/types";

import { applyRunEvent, createRunProjection } from "./runProjection";

function event(seq: number, type: RunEvent["type"], payload: RunEvent["payload"] = {}): RunEvent {
  return {
    run_id: "run_1",
    seq,
    type,
    timestamp: "2026-08-14T00:00:00Z",
    payload,
  };
}

describe("applyRunEvent", () => {
  it("ignores duplicate and backward events", () => {
    const first = applyRunEvent(createRunProjection("run_1"), event(1, "run.started"));
    const duplicate = applyRunEvent(first, event(1, "run.failed", { error_code: "BAD" }));
    const backward = applyRunEvent(duplicate, event(0, "run.failed", { error_code: "BAD" }));

    expect(duplicate).toBe(first);
    expect(backward).toBe(first);
    expect(first.status).toBe("running");
  });

  it("separates cancel requested from canceled terminal state", () => {
    const requested = applyRunEvent(
      createRunProjection("run_1"),
      event(1, "run.cancel_requested", { reason_code: "user_requested" }),
    );
    expect(requested.cancelRequested).toBe(true);
    expect(requested.terminal).toBe(false);

    const canceled = applyRunEvent(requested, event(2, "run.canceled", { error_code: "RUN_CANCELED" }));
    expect(canceled.status).toBe("canceled");
    expect(canceled.terminal).toBe(true);
  });

  it("projects tools and the formal answer reference", () => {
    let state = createRunProjection("run_1");
    state = applyRunEvent(state, event(1, "tool.called", { tool_call_id: "tool_1", tool_name: "sql" }));
    state = applyRunEvent(state, event(2, "tool.succeeded", { tool_call_id: "tool_1", tool_name: "sql", elapsed_ms: 2, evidence_count: 2 }));
    state = applyRunEvent(state, event(3, "answer.ready", {
      assistant_message_id: "message_1",
      artifact_count: 0,
      completion_kind: "partial",
      incomplete_reason: "RUN_TIMEOUT",
    }));

    expect(state.toolStatuses.tool_1).toBe("completed");
    expect(state.answerMessageId).toBe("message_1");
    expect(state.completionKind).toBe("partial");
    expect(state.incompleteReason).toBe("RUN_TIMEOUT");
  });

  it("projects successful Run status without confusing it with completion kind", () => {
    const state = applyRunEvent(createRunProjection("run_1"), event(1, "run.succeeded"));
    expect(state.status).toBe("succeeded");
    expect(state.terminal).toBe(true);
  });

  it("keeps answer provenance from answer.ready without frontend inference", () => {
    const state = applyRunEvent(
      createRunProjection("run_1"),
      event(1, "answer.ready", {
        assistant_message_id: "message_1",
        completion_kind: "completed",
        answer_data_freshness: "current_run_evidence",
        historical_context_injected: true,
        historical_summary_count: 2,
      }),
    );

    expect(state.answerDataFreshness).toBe("current_run_evidence");
    expect(state.historicalContextInjected).toBe(true);
    expect(state.historicalSummaryCount).toBe(2);
  });

  it("does not clear persisted provenance while applying non-answer events", () => {
    const initial = createRunProjection("run_1");
    initial.answerDataFreshness = "not_queried";
    initial.historicalContextInjected = true;
    initial.historicalSummaryCount = 2;

    const state = applyRunEvent(initial, event(1, "run.started"));

    expect(state.historicalContextInjected).toBe(true);
    expect(state.historicalSummaryCount).toBe(2);
  });

  it("projects the protocol selected by the persisted opening event", () => {
    const state = applyRunEvent(
      createRunProjection("run_1"),
      event(1, "run.protocol.selected", {
        protocol_id: "general-task",
        selection_mode: "opening",
      }),
    );
    expect(state.protocolId).toBe("general-task");
  });

  it("累加流式答案并保留草稿直到正式消息刷新", () => {
    let state = createRunProjection("run_1");
    state = applyRunEvent(state, event(1, "run.started"));
    state = applyRunEvent(state, event(2, "answer.delta", { delta: "## 结" }));
    state = applyRunEvent(state, event(3, "answer.delta", { delta: "论" }));
    expect(state.answerDraft).toBe("## 结论");

    state = applyRunEvent(state, event(4, "answer.ready", {
      assistant_message_id: "message_1",
      artifact_count: 0,
      completion_kind: "completed",
    }));
    expect(state.answerDraft).toBe("## 结论");
  });

  it("终态事件丢弃未确认的流式草稿", () => {
    let state = createRunProjection("run_1");
    state = applyRunEvent(state, event(1, "run.started"));
    state = applyRunEvent(state, event(2, "answer.delta", { delta: "候选" }));
    state = applyRunEvent(state, event(3, "run.failed", { error_code: "FINAL_ANSWER_TIMEOUT" }));
    expect(state.answerDraft).toBeNull();
  });

  it("保留失败终态的安全错误说明", () => {
    const state = applyRunEvent(
      createRunProjection("run_1"),
      event(1, "run.failed", {
        error_code: "ANALYSIS_PLAN_INVALID",
        error_message: "分析计划未通过校验，请重试",
      }),
    );

    expect(state.errorCode).toBe("ANALYSIS_PLAN_INVALID");
    expect(state.errorMessage).toBe("分析计划未通过校验，请重试");
  });
});
