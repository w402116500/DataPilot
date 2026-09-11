import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  createRun: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  listSessionMessages: vi.fn(),
  listSessionRuns: vi.fn(),
  listSessions: vi.fn(),
  updateSession: vi.fn(),
}));

vi.mock("@/api/sessions", () => api);

import { useSessionStore } from "./sessionStore";
import type { Session } from "@/api/types";

function session(id: string, title = id): Session {
  return {
    id,
    title,
    selected_datasource_id: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
    last_message_at: null,
  };
}

function emptyPage<T>(items: T[], total = items.length, pageSize = 8) {
  return { items, total, page: 1, page_size: pageSize };
}

describe("sessionStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    api.createRun.mockResolvedValue({ run_id: "run_1", session_id: "session_1", status: "queued" });
    api.updateSession.mockImplementation(async (sessionId: string, payload: { title?: string }) => ({
      id: sessionId,
      title: payload.title ?? "新分析",
      selected_datasource_id: "datasource_1",
      created_at: "2026-08-14T00:00:00Z",
      updated_at: "2026-08-14T00:00:00Z",
      last_message_at: null,
    }));
    api.listSessionMessages.mockResolvedValue(emptyPage([], 0, 20));
    api.listSessionRuns.mockResolvedValue(emptyPage([]));
    api.getSession.mockImplementation(async (sessionId: string) => session(sessionId));
  });

  it("only renames a new session after its first successful question", async () => {
    const store = useSessionStore();
    store.currentSession = {
      id: "session_1",
      title: "新分析",
      selected_datasource_id: "datasource_1",
      created_at: "2026-08-14T00:00:00Z",
      updated_at: "2026-08-14T00:00:00Z",
      last_message_at: null,
    };

    await store.submitRun("请分析 7 月销售额", "key_1");

    expect(api.createRun).toHaveBeenCalledWith("session_1", {
      question: "请分析 7 月销售额",
      idempotency_key: "key_1",
    });
    expect(api.updateSession).toHaveBeenCalledWith("session_1", { title: "请分析 7 月销售额" });

    store.currentSession = { ...store.currentSession, title: "销售复盘" };
    await store.submitRun("请分析 8 月销售额", "key_2");
    expect(api.updateSession).toHaveBeenCalledTimes(1);
  });

  it("loads eight compact sessions and pins the current one when missing", async () => {
    const store = useSessionStore();
    store.currentSession = session("session_old");
    api.listSessions.mockResolvedValue(
      emptyPage(Array.from({ length: 8 }, (_, index) => session(`session_${index + 1}`))),
    );

    await store.load();

    expect(api.listSessions).toHaveBeenCalledWith(1, 8);
    expect(store.sessions.map((item) => item.id)).toEqual([
      "session_old",
      "session_1",
      "session_2",
      "session_3",
      "session_4",
      "session_5",
      "session_6",
      "session_7",
      "session_8",
    ]);
  });

  it("selects the remaining newest session after deleting the current one", async () => {
    const store = useSessionStore();
    store.currentSession = session("session_1");
    api.deleteSession.mockResolvedValue({ deleted: true });
    api.listSessions
      .mockResolvedValueOnce(emptyPage([session("session_2")]))
      .mockResolvedValue(emptyPage([session("session_2")]));

    await store.remove("session_1");

    expect(api.deleteSession).toHaveBeenCalledWith("session_1");
    expect(store.currentSession?.id).toBe("session_2");
    expect(api.listSessionMessages).toHaveBeenCalledWith("session_2", 20);
    expect(api.listSessionRuns).toHaveBeenCalledWith("session_2", 1, 8);
  });

  it("prepends older messages without duplicating the current window", async () => {
    const store = useSessionStore();
    store.currentSession = session("session_1");
    store.messages = [
      {
        id: "message_6",
        session_id: "session_1",
        run_id: null,
        role: "user",
        content_text: "较新",
        answer_evidence_refs: null,
        position: 6,
        created_at: "2026-08-14T00:06:00Z",
      },
    ];
    store.messagesTotal = 6;
    api.listSessionMessages.mockResolvedValue(
      emptyPage(
        [
          {
            id: "message_1",
            session_id: "session_1",
            run_id: null,
            role: "user",
            content_text: "更早",
            answer_evidence_refs: null,
            position: 1,
            created_at: "2026-08-14T00:01:00Z",
          },
          {
            id: "message_6",
            session_id: "session_1",
            run_id: null,
            role: "user",
            content_text: "较新",
            answer_evidence_refs: null,
            position: 6,
            created_at: "2026-08-14T00:06:00Z",
          },
        ],
        6,
        20,
      ),
    );

    const loaded = await store.loadOlderMessages();

    expect(loaded).toBe(true);
    expect(api.listSessionMessages).toHaveBeenCalledWith("session_1", 20, 6);
    expect(store.messages.map((item) => item.id)).toEqual(["message_1", "message_6"]);
  });

  it("does not request older messages when the latest window already starts at position 1", async () => {
    const store = useSessionStore();
    store.currentSession = session("session_1");
    store.messages = [
      {
        id: "message_1",
        session_id: "session_1",
        run_id: null,
        role: "user",
        content_text: "最早",
        answer_evidence_refs: null,
        position: 1,
        created_at: "2026-08-14T00:01:00Z",
      },
    ];
    store.messagesTotal = 1;

    expect(await store.loadOlderMessages()).toBe(false);
    expect(api.listSessionMessages).not.toHaveBeenCalled();
  });

  it("clears the workspace when the last session is deleted", async () => {
    const store = useSessionStore();
    store.currentSession = session("session_1");
    store.messages = [
      {
        id: "message_1",
        session_id: "session_1",
        run_id: null,
        role: "user",
        content_text: "问",
        answer_evidence_refs: null,
        position: 1,
        created_at: "2026-08-14T00:00:00Z",
      },
    ];
    api.deleteSession.mockResolvedValue({ deleted: true });
    api.listSessions.mockResolvedValue(emptyPage([]));

    await store.remove("session_1");

    expect(store.currentSession).toBeNull();
    expect(store.messages).toEqual([]);
    expect(store.sessions).toEqual([]);
  });
});
