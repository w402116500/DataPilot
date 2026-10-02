import type { Message, Run } from "@/api/types";

export function isRunActivityAnchor(
  message: Message,
  run: Pick<Run, "user_message_id"> | null,
): boolean {
  return run !== null && message.role === "user" && run.user_message_id === message.id;
}

export function runForActivityAnchor(
  message: Message,
  currentRun: Pick<Run, "id" | "user_message_id"> | null,
  sessionRuns: readonly Pick<Run, "id" | "user_message_id">[],
): Pick<Run, "id" | "user_message_id"> | null {
  if (message.role !== "user") return null;
  if (isRunActivityAnchor(message, currentRun)) return currentRun;
  return sessionRuns.find((run) => run.user_message_id === message.id) ?? null;
}
