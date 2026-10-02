import { describe, expect, it } from "vitest";

import type { RunTraceEntry } from "./runTrace";
import { presentTraceEntries } from "./tracePresentation";

function entry(seq: number, eventType: RunTraceEntry["eventType"] = "answer.delta", runId = "run_1"): RunTraceEntry {
  return {
    id: `trace:${runId}:${seq}`,
    detailId: `event:${seq}`,
    seq,
    eventType,
    timestamp: "2026-09-05T00:00:00Z",
    title: eventType === "answer.delta" ? "生成答案内容" : eventType,
    summary: "已追加 3 个答案字符。",
    status: "succeeded",
    statusLabel: "已记录",
    toolCallId: null,
    artifactId: null,
    resultSummary: null,
  };
}

describe("presentTraceEntries", () => {
  it("groups consecutive answer fragments without mutating source events or claiming answer completion", () => {
    const source = [entry(9, "final_answer.request.started"), entry(10), entry(11), entry(12), entry(13, "answer.ready")];
    const snapshot = structuredClone(source);
    source.forEach(Object.freeze);
    const rows = presentTraceEntries(Object.freeze(source), "run_1");

    expect(rows).toHaveLength(3);
    expect(rows[0]).toMatchObject(source[0]!);
    expect(rows[1]).toMatchObject({
      id: "trace:run_1:10", detailId: "event:10", seq: 10,
      startSeq: 10, endSeq: 12, fragmentCount: 3,
      title: "生成答案内容", status: "succeeded", statusLabel: "已记录",
      summary: "累计 3 个答案片段（事件 #10 至 #12）。",
    });
    expect(rows[2]).toMatchObject(source[4]!);
    expect(source).toEqual(snapshot);
  });

  it("keeps every non-delta event and separates output attempts at their persisted boundaries", () => {
    const source = [entry(1), entry(2), entry(3, "tool.failed"), entry(4), entry(5), entry(6, "final_answer.request.started"), entry(7), entry(8)];
    source[2]!.status = "failed";
    source[2]!.toolCallId = "tool_1";
    const rows = presentTraceEntries(source, "run_1");
    expect(rows.map((row) => [row.startSeq, row.endSeq, row.fragmentCount])).toEqual([[1, 2, 2], [3, 3, 0], [4, 5, 2], [6, 6, 0], [7, 8, 2]]);
    expect(rows[1]).toMatchObject(source[2]!);
    expect(rows[3]).toMatchObject(source[5]!);
  });

  it("does not bridge missing, duplicate, or out-of-order event sequences", () => {
    const rows = presentTraceEntries([entry(1), entry(3), entry(4), entry(4), entry(2)], "run_1");
    expect(rows.map((row) => [row.startSeq, row.endSeq])).toEqual([[1, 1], [3, 4], [4, 4], [2, 2]]);
  });

  it("does not combine another Run or entries without verified Run identity", () => {
    expect(presentTraceEntries([entry(1), entry(2, "answer.delta", "run_2"), entry(3)], "run_1")).toHaveLength(3);
    expect(presentTraceEntries([entry(1), entry(2)], "")).toHaveLength(2);
    expect(presentTraceEntries([{ ...entry(1), id: "unknown" }, entry(2)], "run_1")).toHaveLength(2);
  });

  it("keeps the original navigation anchor stable as another fragment arrives", () => {
    const source = [entry(10), entry(11)];
    const initial = presentTraceEntries(source, "run_1")[0]!;
    const updated = presentTraceEntries([...source, entry(12)], "run_1")[0]!;
    expect(updated.id).toBe(initial.id);
    expect(updated.detailId).toBe(initial.detailId);
    expect(updated.seq).toBe(initial.seq);
    expect(updated.endSeq).toBe(12);
    expect(initial.endSeq).toBe(11);
  });
});
