import { requestJson } from "./client";
import type {
  DeleteResult,
  Message,
  PageResult,
  Run,
  RunCreate,
  RunCreateAccepted,
  Session,
  SessionCreate,
  SessionUpdate,
} from "./types";

function pageQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export function listSessions(
  page = 1,
  pageSize = 20,
  q?: string,
): Promise<PageResult<Session>> {
  return requestJson<PageResult<Session>>(
    `/sessions${pageQuery({ page, page_size: pageSize, q })}`,
  );
}

export function createSession(payload: SessionCreate): Promise<Session> {
  return requestJson<Session>("/sessions", { method: "POST", body: JSON.stringify(payload) });
}

export function getSession(sessionId: string): Promise<Session> {
  return requestJson<Session>(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function updateSession(sessionId: string, payload: SessionUpdate): Promise<Session> {
  return requestJson<Session>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteSession(sessionId: string): Promise<DeleteResult> {
  return requestJson<DeleteResult>(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export function listSessionMessages(
  sessionId: string,
  pageSize = 20,
  beforePosition?: number,
): Promise<PageResult<Message>> {
  return requestJson<PageResult<Message>>(
    `/sessions/${encodeURIComponent(sessionId)}/messages${pageQuery({
      page_size: pageSize,
      before_position: beforePosition,
    })}`,
  );
}

export function createRun(sessionId: string, payload: RunCreate): Promise<RunCreateAccepted> {
  return requestJson<RunCreateAccepted>(`/sessions/${encodeURIComponent(sessionId)}/runs`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listSessionRuns(
  sessionId: string,
  page = 1,
  pageSize = 20,
  q?: string,
): Promise<PageResult<Run>> {
  return requestJson<PageResult<Run>>(
    `/sessions/${encodeURIComponent(sessionId)}/runs${pageQuery({
      page,
      page_size: pageSize,
      q,
    })}`,
  );
}
