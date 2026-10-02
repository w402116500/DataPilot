import { describe, expect, it } from "vitest";

import {
  canDockConsole,
  clampConsoleWidth,
  clampLeftWidth,
  deriveConsoleMaxWidth,
  nextEscapeAction,
  resolveWorkspaceLayout,
} from "./workspaceLayout";

describe("workspace layout rules", () => {
  it("uses chat minimum plus both sidebars as the dock threshold", () => {
    expect(canDockConsole(958, 238, 300)).toBe(true);
    expect(canDockConsole(957, 238, 300)).toBe(false);
    expect(resolveWorkspaceLayout({ containerWidth: 957, leftWidth: 238, consoleWidth: 300 }).isNarrow).toBe(true);
  });

  it("derives a bounded console maximum from available chat space", () => {
    expect(deriveConsoleMaxWidth(1_600, 238)).toBe(520);
    expect(deriveConsoleMaxWidth(1_050, 238)).toBe(392);
    expect(deriveConsoleMaxWidth(700, 238)).toBe(300);
  });

  it("clamps left and console widths without allowing an unreadable chat", () => {
    expect(clampLeftWidth(100)).toBe(208);
    expect(clampLeftWidth(500)).toBe(320);
    expect(clampConsoleWidth(100, 1_200, 238)).toBe(300);
    expect(clampConsoleWidth(900, 1_200, 238)).toBe(520);
    expect(clampConsoleWidth(500, 1_050, 238)).toBe(392);
  });

  it("prioritizes nested escape targets", () => {
    expect(nextEscapeAction({ previewOpen: true, artifactPageOpen: true, drawerOpen: true })).toBe("close-preview");
    expect(nextEscapeAction({ previewOpen: false, artifactPageOpen: true, drawerOpen: true })).toBe("close-artifact");
    expect(nextEscapeAction({ previewOpen: false, artifactPageOpen: false, drawerOpen: true })).toBe("close-drawer");
    expect(nextEscapeAction({ previewOpen: false, artifactPageOpen: false, drawerOpen: false })).toBe("none");
  });
});
