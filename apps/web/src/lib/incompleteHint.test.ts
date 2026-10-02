import { describe, expect, it } from "vitest";

import { incompleteHint } from "./incompleteHint";

describe("incompleteHint", () => {
  it("gives CONTEXT_BUDGET_EXHAUSTED a distinct sentence", () => {
    expect(incompleteHint("CONTEXT_BUDGET_EXHAUSTED")).toBe(
      "上下文预算不足，最终回答未生成。",
    );
    expect(`本次回答不完整：${incompleteHint("CONTEXT_BUDGET_EXHAUSTED")}`).not.toContain(
      "本次回答不完整：本次回答不完整",
    );
  });

  it("keeps the unknown-reason fallback from duplicating the page prefix", () => {
    expect(incompleteHint("UNKNOWN_REASON")).toBe("本次回答不完整，请结合过程和产物复核。");
  });
});
