import type { RunTraceEntry } from "./runTrace";

export interface TracePresentationEntry extends RunTraceEntry {
  startSeq: number;
  endSeq: number;
  fragmentCount: number;
}

/** Groups only an uninterrupted stream in one Run; the source trace stays intact. */
export function presentTraceEntries(entries: readonly RunTraceEntry[], runId: string): TracePresentationEntry[] {
  const presented: TracePresentationEntry[] = [];
  const belongsToRun = (entry: RunTraceEntry) => runId.length > 0 && entry.id === `trace:${runId}:${entry.seq}`;
  for (const entry of entries) {
    const previous = presented[presented.length - 1];
    if (entry.eventType === "answer.delta" && previous?.eventType === "answer.delta"
      && belongsToRun(entry) && belongsToRun(previous) && entry.seq === previous.endSeq + 1) {
      previous.endSeq = entry.seq;
      previous.fragmentCount += 1;
      previous.summary = `累计 ${previous.fragmentCount} 个答案片段（事件 #${previous.startSeq} 至 #${previous.endSeq}）。`;
    } else {
      presented.push({
        ...entry,
        startSeq: entry.seq,
        endSeq: entry.seq,
        fragmentCount: entry.eventType === "answer.delta" ? 1 : 0,
      });
    }
  }
  return presented;
}
