import { computed, ref } from "vue";
import { defineStore } from "pinia";

import {
  createRun,
  createSession,
  deleteSession,
  getSession,
  listSessionMessages,
  listSessionRuns,
  listSessions,
  updateSession,
} from "@/api/sessions";
import type { Message, Run, Session } from "@/api/types";

export const COMPACT_LIST_SIZE = 8;
export const MESSAGE_WINDOW_SIZE = 20;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "会话读取失败";
}

function pinCurrentSession(items: Session[], current: Session | null): Session[] {
  if (current === null) return items;
  if (items.some((item) => item.id === current.id)) return items;
  return [current, ...items];
}

export const useSessionStore = defineStore("session", () => {
  const sessions = ref<Session[]>([]);
  const currentSession = ref<Session | null>(null);
  const messages = ref<Message[]>([]);
  const messagesTotal = ref(0);
  const runs = ref<Run[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const hasSessions = computed(() => sessions.value.length > 0);

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const page = await listSessions(1, COMPACT_LIST_SIZE);
      sessions.value = pinCurrentSession(page.items, currentSession.value);
    } catch (caught) {
      error.value = errorMessage(caught);
    } finally {
      loading.value = false;
    }
  }

  async function select(sessionId: string): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const [session, messagePage, runPage, sessionPage] = await Promise.all([
        getSession(sessionId),
        listSessionMessages(sessionId, MESSAGE_WINDOW_SIZE),
        listSessionRuns(sessionId, 1, COMPACT_LIST_SIZE),
        listSessions(1, COMPACT_LIST_SIZE),
      ]);
      if (session === null) throw new Error("会话不存在");
      currentSession.value = session;
      messages.value = messagePage.items;
      messagesTotal.value = messagePage.total;
      runs.value = runPage.items;
      sessions.value = pinCurrentSession(sessionPage.items, session);
    } catch (caught) {
      error.value = errorMessage(caught);
    } finally {
      loading.value = false;
    }
  }

  async function create(title = "新分析", datasourceId: string | null = null): Promise<Session> {
    const session = await createSession({ title, selected_datasource_id: datasourceId });
    currentSession.value = session;
    messages.value = [];
    messagesTotal.value = 0;
    runs.value = [];
    sessions.value = pinCurrentSession(
      [session, ...sessions.value.filter((item) => item.id !== session.id)].slice(
        0,
        COMPACT_LIST_SIZE,
      ),
      session,
    );
    return session;
  }

  async function rename(title: string): Promise<void> {
    if (currentSession.value === null) return;
    const session = await updateSession(currentSession.value.id, { title });
    currentSession.value = session;
    sessions.value = sessions.value.map((item) => (item.id === session.id ? session : item));
  }

  async function chooseDatasource(datasourceId: string | null): Promise<void> {
    if (currentSession.value === null) return;
    const session = await updateSession(currentSession.value.id, {
      selected_datasource_id: datasourceId,
    });
    currentSession.value = session;
    sessions.value = sessions.value.map((item) => (item.id === session.id ? session : item));
  }

  async function submitRun(question: string, idempotencyKey: string): Promise<string> {
    if (currentSession.value === null) throw new Error("请先选择会话");
    const accepted = await createRun(currentSession.value.id, {
      question,
      idempotency_key: idempotencyKey,
    });
    if (currentSession.value.title === "新分析") {
      await rename(question.trim().slice(0, 20) || "新分析");
    }
    return accepted.run_id;
  }

  function clearWorkspace(): void {
    currentSession.value = null;
    messages.value = [];
    messagesTotal.value = 0;
    runs.value = [];
  }

  async function loadOlderMessages(): Promise<boolean> {
    const sessionId = currentSession.value?.id;
    const first = messages.value[0];
    if (sessionId === undefined || first === undefined) return false;
    if (messages.value.length >= messagesTotal.value || first.position <= 1) return false;
    const page = await listSessionMessages(sessionId, MESSAGE_WINDOW_SIZE, first.position);
    if (currentSession.value?.id !== sessionId) return false;
    const existing = new Set(messages.value.map((item) => item.id));
    const older = page.items.filter((item) => !existing.has(item.id));
    messagesTotal.value = page.total;
    if (older.length === 0) return false;
    messages.value = [...older, ...messages.value];
    return true;
  }

  async function remove(sessionId: string): Promise<void> {
    const wasCurrent = currentSession.value?.id === sessionId;
    await deleteSession(sessionId);
    const page = await listSessions(1, COMPACT_LIST_SIZE);
    if (wasCurrent) {
      const next = page.items[0];
      if (next === undefined) {
        clearWorkspace();
        sessions.value = [];
        return;
      }
      await select(next.id);
      return;
    }
    sessions.value = pinCurrentSession(page.items, currentSession.value);
  }

  return {
    sessions,
    currentSession,
    messages,
    messagesTotal,
    runs,
    loading,
    error,
    hasSessions,
    load,
    select,
    create,
    rename,
    chooseDatasource,
    submitRun,
    loadOlderMessages,
    remove,
  };
});
