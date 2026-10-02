import { createApp, h, nextTick, reactive } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DataLinkVersion, DataLinkVersions } from "@/api/types";
import DataLinkVersionHistory from "./DataLinkVersionHistory.vue";

const api = vi.hoisted(() => ({ versions: vi.fn(), conflicts: vi.fn(), catalog: vi.fn(), restore: vi.fn(), resolve: vi.fn(), diff: vi.fn() }));
vi.mock("@/api/datasources", () => ({ getDatalinkVersions: api.versions, getDatalinkVersionConflicts: api.conflicts, getDatalinkVersionCatalog: api.catalog, getDatalinkVersionDiff: api.diff, restoreDatalinkVersion: api.restore, resolveDatalinkCandidate: api.resolve }));
const old: DataLinkVersion = { build_id: "b1", graph_version: "old", schema_revision: 3, status: "completed", origin_kind: "automated", publication_state: "published", base_graph_version: null, source_graph_version: null, created_at: "2026-09-05T01:00:00Z", finished_at: null, is_head: false, conflict_count: 0 };
const candidate: DataLinkVersion = { ...old, graph_version: "candidate", publication_state: "candidate", conflict_count: 1 };
let cleanup: (() => void) | undefined;
async function settle() { for (let i = 0; i < 5; i += 1) { await Promise.resolve(); await nextTick(); } }
function mount() {
  const props = reactive({ datasourceId: "ds", graphVersion: "head", schemaRevision: 3, hasUnsavedChanges: false });
  const published = vi.fn(); const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(DataLinkVersionHistory, { ...props, onPublished: published }) }); app.mount(host);
  cleanup = () => { app.unmount(); host.remove(); };
  return { props, published };
}
function button(text: string): HTMLButtonElement {
  const result = Array.from(document.querySelectorAll("button")).find((item) => item.textContent?.includes(text));
  expect(result).toBeDefined(); return result!;
}
async function choose(label: string, value: string): Promise<void> {
  const select = document.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!;
  select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); await settle();
}
async function confirm(): Promise<void> {
  document.querySelector<HTMLInputElement>('[aria-label="确认发布版本变更"]')!.click(); await settle();
}
beforeEach(() => {
  api.versions.mockReset().mockResolvedValue({ datasource_id: "ds", items: [old, candidate], total: 2, page: 1, page_size: 20 });
  api.conflicts.mockReset().mockResolvedValue({ datasource_id: "ds", candidate_graph_version: "candidate", base_graph_version: "head", schema_revision: 3, items: [{ object_key: "edge", object_kind: "relation", name: "客户关联", reason: "字段缺失", node_type: null, edge_type: "joinable" }] });
  api.catalog.mockReset().mockResolvedValue({ datasource_id: "ds", graph_version: "candidate", total: 3, items: ["a", "b", "c"].map((id) => ({ node: { id, name: id, table: id === "b" ? "customers" : "orders", type: "column", aliases: [], description: null, semantic_type: null, profile: null }, provenance: "structural", mapping_count: 0, relation_count: 0 })) });
  api.restore.mockReset().mockResolvedValue({ graph_version: "restored" }); api.resolve.mockReset().mockResolvedValue({ graph_version: "resolved" });
  api.diff.mockReset().mockResolvedValue({ datasource_id: "ds", graph_version: "old", base_graph_version: "head", origin_kind: "automated", publication_state: "published", truncated: false, items: [{ kind: "node_updated", object_key: "a", object_kind: "node", name: "orders.amount", automatic: { name: "amount", description: "自动说明", aliases: ["实付"], semantic_type: null }, effective: { name: "amount", description: "订单原价", aliases: ["成交额"], semantic_type: null }, before_targets: [], after_targets: [] }] });
});
afterEach(() => { cleanup?.(); cleanup = undefined; });

describe("DataLink version history", () => {
  it("requires explicit confirmation before restoring the selected version", async () => {
    const { published } = mount(); await settle(); button("恢复配置").click(); await settle();
    expect(button("恢复并发布").disabled).toBe(true); expect(api.restore).not.toHaveBeenCalled();
    await confirm(); button("恢复并发布").click(); await settle();
    expect(api.restore).toHaveBeenCalledWith("ds", { target_graph_version: "old", expected_head: "head", schema_revision: 3, idempotency_key: expect.any(String) }, expect.any(AbortSignal));
    expect(published).toHaveBeenCalledOnce();
  });
  it("does not default to discarding conflicts and uses real candidate fields for rebind", async () => {
    mount(); await settle(); button("处理冲突").click(); await settle();
    await confirm(); expect(button("应用处理并发布").disabled).toBe(true);
    await choose("客户关联处理方式", "rebind"); await choose("客户关联起点字段", "a"); await choose("客户关联终点字段", "b");
    expect(api.catalog).toHaveBeenCalledWith("ds", "candidate", "column", 1, expect.any(AbortSignal));
    expect(button("应用处理并发布").disabled).toBe(true);
    await confirm(); button("应用处理并发布").click(); await settle();
    expect(api.resolve.mock.calls[0][1]).toMatchObject({ candidate_graph_version: "candidate", expected_head: "head", resolutions: [{ object_key: "edge", action: "rebind", source_id: "a", target_id: "b" }] });
  });
  it("blocks publication while the semantic editor has unsaved changes", async () => {
    const { props } = mount(); await settle(); button("恢复配置").click(); await settle(); await confirm();
    props.hasUnsavedChanges = true; await settle(); expect(button("恢复并发布").disabled).toBe(true);
  });
  it("rejects relation bindings within the same table before publication", async () => {
    mount(); await settle(); button("处理冲突").click(); await settle();
    await choose("客户关联处理方式", "rebind"); await choose("客户关联起点字段", "a"); await choose("客户关联终点字段", "c");
    await confirm(); expect(button("应用处理并发布").disabled).toBe(true);
    expect(api.resolve).not.toHaveBeenCalled();
  });
  it("ignores a completed restore response after switching datasource", async () => {
    let resolve!: (value: unknown) => void;
    api.restore.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { props, published } = mount(); await settle(); button("恢复配置").click(); await settle(); await confirm();
    button("恢复并发布").click(); await settle();
    const signal: AbortSignal = api.restore.mock.calls[0][2];
    api.versions.mockResolvedValue({ datasource_id: "other", items: [], total: 0, page: 1, page_size: 20 });
    props.datasourceId = "other"; await settle();
    expect(signal.aborted).toBe(true);
    resolve({ graph_version: "restored" }); await settle();
    expect(published).not.toHaveBeenCalled();
    expect(document.querySelector('[aria-label="确认发布版本变更"]')).toBeNull();
  });
  it("reuses the restoration key after an uncertain network failure", async () => {
    api.restore.mockRejectedValueOnce(new Error("timeout")); mount(); await settle(); button("恢复配置").click(); await settle(); await confirm();
    button("恢复并发布").click(); await settle(); button("恢复并发布").click(); await settle();
    expect(api.restore.mock.calls[0][1].idempotency_key).toBe(api.restore.mock.calls[1][1].idempotency_key);
  });
  it("shows snapshot semantic diffs without a restore confirmation", async () => {
    mount(); await settle(); button("查看差异").click(); await settle();
    expect(api.diff).toHaveBeenCalledWith("ds", "old", expect.any(AbortSignal));
    expect(document.body.textContent).toContain("修改业务语义");
    expect(document.body.textContent).toContain("自动说明");
    expect(document.body.textContent).toContain("订单原价");
    expect(document.querySelector('[aria-label="确认发布版本变更"]')).toBeNull();
    expect(document.body.textContent).not.toContain("恢复并发布");
  });
  it("rejects a late version list response from another datasource", async () => {
    let resolve!: (value: DataLinkVersions) => void;
    api.versions.mockImplementationOnce(() => new Promise<DataLinkVersions>((done) => { resolve = done; }));
    const { props } = mount(); api.versions.mockResolvedValue({ datasource_id: "other", items: [], total: 0, page: 1, page_size: 20 });
    props.datasourceId = "other"; await settle(); resolve({ datasource_id: "ds", items: [old], total: 1, page: 1, page_size: 20 }); await settle();
    expect(document.body.textContent).toContain("暂无版本记录"); expect(document.body.textContent).not.toContain("自动构建");
  });
});
