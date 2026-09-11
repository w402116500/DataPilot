import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useWorkspaceStore } from "./workspaceStore";

describe("workspaceStore layout state", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("accepts the runs console tab", () => {
    const workspace = useWorkspaceStore();
    workspace.setConsoleTab("runs");
    expect(workspace.consoleTab).toBe("runs");
  });

  it("remembers explicit console close per session and releases it on detail action", () => {
    const workspace = useWorkspaceStore();
    expect(workspace.isConsoleSuppressed("session-1")).toBe(false);
    workspace.suppressConsole("session-1");
    expect(workspace.isConsoleSuppressed("session-1")).toBe(true);
    expect(workspace.isConsoleSuppressed("session-2")).toBe(false);
    workspace.releaseConsole("session-1");
    expect(workspace.isConsoleSuppressed("session-1")).toBe(false);
  });

  it("keeps width preferences inside layout bounds as the container changes", () => {
    const workspace = useWorkspaceStore();
    workspace.setContainerWidth(1_600);
    workspace.setLeftSidebarWidth(300);
    workspace.setConsoleWidth(500);
    expect(workspace.leftSidebarWidth).toBe(300);
    expect(workspace.consoleWidth).toBe(500);
    workspace.setContainerWidth(1_030);
    expect(workspace.consoleWidth).toBe(310);
    expect(workspace.consoleDockable).toBe(true);
    workspace.setContainerWidth(950);
    expect(workspace.consoleDockable).toBe(false);
    expect(workspace.isNarrow).toBe(true);
  });
});
