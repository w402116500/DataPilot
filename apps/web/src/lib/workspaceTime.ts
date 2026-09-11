const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const HAS_EXPLICIT_OFFSET = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export function parseApiInstant(iso: string): Date | null {
  const trimmed = iso.trim();
  if (!trimmed) return null;
  // Metadata 时间是 UTC。SQLite 读出后常丢失 tzinfo，JSON 可能没有 Z；
  // 浏览器会把无时区 ISO 当成本地时间，东八区会整段偏 8 小时。
  const normalized = HAS_EXPLICIT_OFFSET.test(trimmed) ? trimmed : `${trimmed}Z`;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? new Date(parsed) : null;
}

function startOfLocalDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

export function localDayKey(iso: string): string {
  const date = parseApiInstant(iso);
  if (date === null) return "";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function relativeActivityLabel(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "新会话";
  const date = parseApiInstant(iso);
  if (date === null) return "新会话";
  const delta = now.getTime() - date.getTime();
  if (delta < MINUTE) return "刚刚";
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)} 分钟前`;
  if (startOfLocalDay(date) === startOfLocalDay(now) && delta < DAY) {
    return `${Math.floor(delta / HOUR)} 小时前`;
  }
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (startOfLocalDay(date) === startOfLocalDay(yesterday)) return "昨天";
  return localDayKey(iso);
}

export function wallClockLabel(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const date = parseApiInstant(iso);
  if (date === null) return "";
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (startOfLocalDay(date) === startOfLocalDay(now)) return time;
  return `${localDayKey(iso)} ${time}`;
}

export function runStartLabel(
  run: { started_at: string | null; created_at: string },
  now = new Date(),
): string {
  return wallClockLabel(run.started_at ?? run.created_at, now);
}

export function messageClockLabel(iso: string): string {
  const date = parseApiInstant(iso);
  if (date === null) return "";
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function daySeparatorLabel(iso: string, now = new Date()): string {
  const date = parseApiInstant(iso);
  if (date === null) return "";
  if (startOfLocalDay(date) === startOfLocalDay(now)) return "今天";
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (startOfLocalDay(date) === startOfLocalDay(yesterday)) return "昨天";
  return localDayKey(iso);
}
