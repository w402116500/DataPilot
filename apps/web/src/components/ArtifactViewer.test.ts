import { createApp, nextTick, type App } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BinaryResponse } from "@/api/client";
import type { RunArtifact } from "@/api/types";
import { useArtifactStore } from "@/stores/artifactStore";

import ArtifactViewer from "./ArtifactViewer.vue";

const mountedApps: App[] = [];
const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function restoreUrlMethod(name: "createObjectURL" | "revokeObjectURL", descriptor?: PropertyDescriptor): void {
  if (descriptor === undefined) {
    Reflect.deleteProperty(URL, name);
    return;
  }
  Object.defineProperty(URL, name, descriptor);
}

function artifact(overrides: Partial<RunArtifact> = {}): RunArtifact {
  return {
    id: "artifact_1",
    run_id: "run_1",
    session_id: "session_1",
    tool_call_id: "tool_1",
    datasource_deleted: false,
    type: "file",
    title: "result.csv",
    mime_type: "text/csv",
    size_bytes: 64,
    inline_previewable: true,
    preview: null,
    metadata: null,
    content_hash: "hash_1",
    created_at: "2026-08-16T00:00:00Z",
    ...overrides,
  };
}

async function binaryResponse(content: string, contentType: string): Promise<BinaryResponse> {
  const blob = await new Response(content, { headers: { "content-type": contentType } }).blob();
  if (typeof blob.text !== "function") {
    Object.defineProperty(blob, "text", { value: async () => content });
  }
  return {
    blob,
    contentType,
    filename: null,
  };
}

async function mountViewer(
  selectedArtifact: RunArtifact,
  content: string | null,
  props: { maxRows?: number; compact?: boolean; hideDownload?: boolean } = {},
): Promise<HTMLElement> {
  const pinia = createPinia();
  setActivePinia(pinia);
  if (content !== null) {
    const store = useArtifactStore();
    store.contentById = {
      [selectedArtifact.id]: await binaryResponse(content, selectedArtifact.mime_type),
    };
  }
  const app = createApp(ArtifactViewer, { artifact: selectedArtifact, ...props });
  app.use(pinia);
  const host = document.createElement("div");
  document.body.append(host);
  app.mount(host);
  mountedApps.push(app);
  await nextTick();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  await nextTick();
  return host;
}

function buttonWithText(host: HTMLElement, text: string): HTMLButtonElement | undefined {
  return [...host.querySelectorAll<HTMLButtonElement>("button")]
    .find((button) => button.textContent?.includes(text));
}

describe("ArtifactViewer", () => {
  afterEach(() => {
    for (const app of mountedApps.splice(0)) app.unmount();
    document.body.replaceChildren();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    restoreUrlMethod("createObjectURL", originalCreateObjectURL);
    restoreUrlMethod("revokeObjectURL", originalRevokeObjectURL);
  });

  it("searches, sorts, and paginates CSV content on the full artifact page", async () => {
    const host = await mountViewer(
      artifact(),
      "name,amount\nBeta,10\nAlpha,2\nGamma,5\n",
      { maxRows: 2 },
    );

    expect(host.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(host.querySelector("tbody")?.textContent).toContain("Beta");
    buttonWithText(host, "下一页")?.click();
    await nextTick();
    expect(host.querySelector("tbody")?.textContent).toContain("Gamma");

    const search = host.querySelector<HTMLInputElement>('input[type="search"]');
    expect(search).not.toBeNull();
    if (search === null) throw new Error("Expected table search input");
    search.value = "alpha";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    await nextTick();
    expect(host.querySelector("tbody")?.textContent).toContain("Alpha");
    expect(host.querySelector("tbody")?.textContent).not.toContain("Beta");

    search.value = "";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    await nextTick();
    host.querySelector<HTMLButtonElement>('button[aria-label="按 amount 升序排列"]')?.click();
    await nextTick();
    expect(host.querySelector("tbody tr")?.textContent).toContain("Alpha");
  });

  it("shows an explicit error for invalid JSON", async () => {
    const host = await mountViewer(
      artifact({ title: "result.json", mime_type: "application/json" }),
      "{broken",
    );

    expect(host.textContent).toContain("JSON 文件格式无效");
  });

  it("formats JSON and reads plain text in code surfaces", async () => {
    const jsonHost = await mountViewer(
      artifact({ id: "artifact_json", title: "result.json", mime_type: "application/json" }),
      '{"ok":true}',
    );
    const textHost = await mountViewer(
      artifact({ id: "artifact_text", title: "notes.txt", mime_type: "text/plain" }),
      "第一行\n第二行",
    );

    expect(jsonHost.querySelector(".code-artifact")?.textContent).toContain('"ok": true');
    expect(textHost.querySelector(".code-artifact")?.textContent).toContain("第一行\n第二行");
  });

  it("renders registered SVG as an image and releases its Object URL", async () => {
    const createObjectURL = vi.fn(() => "blob:artifact-svg");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const host = await mountViewer(
      artifact({
        type: "chart",
        title: "chart.svg",
        mime_type: "image/svg+xml",
      }),
      '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>',
    );

    expect(host.querySelector("img")?.getAttribute("src")).toBe("blob:artifact-svg");
    mountedApps.pop()?.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:artifact-svg");
  });

  it("does not request content for download-only or oversized artifacts", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    const host = await mountViewer(
      artifact({ inline_previewable: false, size_bytes: 2 * 1024 * 1024 }),
      null,
      { compact: true, hideDownload: true },
    );

    expect(host.textContent).toContain("此产物仅支持下载");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
