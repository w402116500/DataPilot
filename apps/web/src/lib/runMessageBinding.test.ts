import { describe, expect, it } from "vitest";

import type { Message, Run } from "@/api/types";

import { isRunActivityAnchor, runForActivityAnchor } from "./runMessageBinding";

const run: Pick<Run, "user_message_id"> = { user_message_id: "message_user_1" };

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "message_user_1",
    session_id: "session_1",
    run_id: null,
    role: "user",
    content_text: "统计订单",
    answer_evidence_refs: null,
    position: 1,
    created_at: "2026-08-28T00:00:00Z",
    ...overrides,
  };
}

describe("isRunActivityAnchor", () => {
  it("anchors activity to the matching user message", () => {
    expect(isRunActivityAnchor(message(), run)).toBe(true);
  });

  it("does not anchor to another user message", () => {
    expect(isRunActivityAnchor(message({ id: "message_user_2" }), run)).toBe(false);
  });

  it("does not anchor to an assistant message", () => {
    expect(isRunActivityAnchor(message({ role: "assistant" }), run)).toBe(false);
  });

  it("does not guess an anchor when the run has no user message id", () => {
    expect(isRunActivityAnchor(message(), { user_message_id: null })).toBe(false);
    expect(isRunActivityAnchor(message(), null)).toBe(false);
  });
});

describe("runForActivityAnchor", () => {
  const current = { id: "run_current", user_message_id: "message_user_1" };
  const historical = { id: "run_old", user_message_id: "message_user_2" };

  it("prefers the current run when it matches the user message", () => {
    expect(runForActivityAnchor(message(), current, [historical, current])).toEqual(current);
  });

  it("anchors a historical run when the current run belongs to another message", () => {
    expect(runForActivityAnchor(
      message({ id: "message_user_2" }),
      current,
      [historical, current],
    )).toEqual(historical);
  });

  it("does not invent an anchor for assistant messages", () => {
    expect(runForActivityAnchor(message({ role: "assistant" }), current, [current])).toBeNull();
  });
});
