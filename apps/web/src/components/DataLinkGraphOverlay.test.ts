import { createApp, h, nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import DataLinkGraphOverlay from "./DataLinkGraphOverlay.vue";

describe("DataLinkGraphOverlay", () => {
  afterEach(() => document.body.replaceChildren());

  it.each(["Escape", "close button", "backdrop"])("closes from %s and restores focus", async (action) => {
    const updates: boolean[] = [];
    const open = ref(false);
    const app = createApp({
      render: () => h(DataLinkGraphOverlay, {
        open: open.value,
        rootName: "customers",
        graphVersion: "graph_1",
        "onUpdate:open": (value: boolean) => {
          updates.push(value);
          open.value = value;
        },
      }),
    });
    const host = document.createElement("div");
    const opener = document.createElement("button");
    opener.textContent = "全屏查看";
    document.body.append(opener, host);
    app.mount(host);
    await nextTick();
    opener.focus();
    open.value = true;
    await vi.waitFor(() => expect(document.activeElement?.getAttribute("title")).toBe("关闭语义地图"));

    if (action === "Escape") {
      document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    } else if (action === "close button") {
      document.body.querySelector<HTMLButtonElement>('[title="关闭语义地图"]')?.click();
    } else {
      document.body.querySelector(".graph-overlay-backdrop")?.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    }

    await vi.waitFor(() => expect(document.body.querySelector('[role="dialog"]')).toBeNull());
    expect(updates).toEqual([false]);
    expect(document.activeElement).toBe(opener);
    app.unmount();
  });

  it("portals outside filtered parents and keeps keyboard focus inside the fullscreen dialog", async () => {
    const host = document.createElement("div");
    host.style.backdropFilter = "blur(20px)";
    host.style.overflow = "hidden";
    const outsideButton = document.createElement("button");
    document.body.append(outsideButton, host);
    const app = createApp(DataLinkGraphOverlay, {
      open: true,
      rootName: "customers",
      graphVersion: "graph_1",
    });
    app.mount(host);
    await vi.waitFor(() => expect(document.activeElement?.getAttribute("title")).toBe("关闭语义地图"));

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!;
    const closeButton = dialog.querySelector<HTMLButtonElement>('[title="关闭语义地图"]')!;
    expect(host.contains(dialog)).toBe(false);
    expect(document.getElementById(dialog.getAttribute("aria-labelledby")!)?.textContent).toBe("语义地图");
    expect(dialog.textContent).toContain("customers");
    expect(dialog.textContent).toContain("graph_1");

    outsideButton.focus();
    expect(document.activeElement).toBe(closeButton);
    app.unmount();
  });
});
