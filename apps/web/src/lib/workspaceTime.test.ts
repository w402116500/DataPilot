import { describe, expect, it } from "vitest";

import {
  daySeparatorLabel,
  localDayKey,
  messageClockLabel,
  parseApiInstant,
  relativeActivityLabel,
  runStartLabel,
  wallClockLabel,
} from "./workspaceTime";

const now = new Date("2026-09-08T15:00:00.000Z");

function localClock(iso: string): string {
  const date = parseApiInstant(iso);
  if (date === null) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

describe("workspaceTime", () => {
  it("treats timezone-less Metadata timestamps as UTC", () => {
    const naive = parseApiInstant("2026-09-08T13:21:09.779821");
    const zulu = parseApiInstant("2026-09-08T13:21:09.779821Z");
    const offset = parseApiInstant("2026-09-08T13:21:09.779821+00:00");
    expect(naive?.getTime()).toBe(Date.parse("2026-09-08T13:21:09.779821Z"));
    expect(zulu?.getTime()).toBe(naive?.getTime());
    expect(offset?.getTime()).toBe(naive?.getTime());
  });

  it("uses last_message_at for relative session activity", () => {
    expect(relativeActivityLabel(null, now)).toBe("新会话");
    expect(relativeActivityLabel("2026-09-08T14:59:30", now)).toBe("刚刚");
    expect(relativeActivityLabel("2026-09-08T14:59:30Z", now)).toBe("刚刚");
    expect(relativeActivityLabel("2026-09-08T14:40:00", now)).toBe("20 分钟前");
    const localYesterday = new Date(now);
    localYesterday.setDate(localYesterday.getDate() - 1);
    localYesterday.setHours(12, 0, 0, 0);
    expect(relativeActivityLabel(localYesterday.toISOString(), now)).toBe("昨天");
  });

  it("does not show an 8-hour lag for a just-updated UTC timestamp", () => {
    const observed = new Date("2026-09-08T13:28:33.000Z");
    expect(relativeActivityLabel("2026-09-08T13:21:09.779821", observed)).toBe("7 分钟前");
  });

  it("shows run wall-clock start time in local time from UTC instants", () => {
    expect(runStartLabel({ started_at: "2026-09-08T14:32:00", created_at: "2026-09-08T14:00:00" }, now)).toBe(
      localClock("2026-09-08T14:32:00Z"),
    );
    expect(runStartLabel({ started_at: null, created_at: "2026-09-07T09:05:00Z" }, now)).toBe(
      `${localDayKey("2026-09-07T09:05:00Z")} ${localClock("2026-09-07T09:05:00Z")}`,
    );
    expect(wallClockLabel("2026-09-08T14:35:00Z", now)).toBe(localClock("2026-09-08T14:35:00Z"));
  });

  it("formats conversation clocks and day separators", () => {
    expect(messageClockLabel("2026-09-08T14:32:00")).toBe(localClock("2026-09-08T14:32:00Z"));
    expect(daySeparatorLabel(now.toISOString(), now)).toBe("今天");
    const localYesterday = new Date(now);
    localYesterday.setDate(localYesterday.getDate() - 1);
    localYesterday.setHours(23, 0, 0, 0);
    expect(daySeparatorLabel(localYesterday.toISOString(), now)).toBe("昨天");
  });
});
