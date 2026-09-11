import { createApp, h, nextTick, reactive, ref } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DataLinkDraft } from "@/api/types";
import DataLinkRevisionEditor from "./DataLinkRevisionEditor.vue";

const api = vi.hoisted(() => ({ draft: vi.fn(), catalog: vi.fn(), detail: vi.fn(), save: vi.fn(), publish: vi.fn(), preview: vi.fn() }));
vi.mock("@/api/datasources", () => ({ getDatalinkDraft: api.draft, getDatalinkCatalog: api.catalog, getDatalinkCatalogDetail: api.detail, getDatalinkRelations: vi.fn().mockResolvedValue({ items: [], total: 0 }), saveDatalinkDraft: api.save, publishDatalinkDraft: api.publish, previewDatalinkDraft: api.preview }));
const saved: DataLinkDraft = { datasource_id: "ds", base_graph_version: "g1", schema_revision: 3, draft_revision: 2, status: "active", changes: [{ change_type: "disable_relation", object_key: "edge1" }] };
let cleanup: (() => void) | undefined;
async function settle() { for (let i = 0; i < 5; i += 1) { await Promise.resolve(); await nextTick(); } }
function mount() {
  const props = reactive({ datasourceId: "ds", graphVersion: "g1", schemaRevision: 3 });
  const published = vi.fn();
  const editor = ref<InstanceType<typeof DataLinkRevisionEditor> | null>(null);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(DataLinkRevisionEditor, { ...props, ref: editor, onPublished: published }) }); app.mount(host);
  cleanup = () => { app.unmount(); host.remove(); };
  return { props, published, editor };
}
function button(text: string): HTMLButtonElement {
  const result = Array.from(document.querySelectorAll("button")).find((item) => item.textContent?.includes(text));
  expect(result).toBeDefined(); return result!;
}
function column(id: string) {
  return { node: { id, name: id, table: "orders", type: "column" as const, aliases: [] as string[], description: id === "a" ? "原说明" : null, semantic_type: null, profile: null }, provenance: "structural" as const, mapping_count: 0, relation_count: 0, can_reset_node: id === "a", can_reset_mapping: id === "a", automatic: id === "a" ? { name: "a", description: "自动说明", aliases: ["实付"], semantic_type: null } : null };
}
function concept(id: string, name: string) {
  return { node: { id, name, table: null, type: "concept" as const, aliases: [] as string[], description: null, semantic_type: null, profile: null }, provenance: "structural" as const, mapping_count: 0, relation_count: 0 };
}
beforeEach(() => {
  api.draft.mockReset().mockResolvedValue(null);
  api.catalog.mockReset().mockImplementation(async (_id: string, options?: { type?: string; page?: number }) => {
    if (options?.type === "concept") {
      const items = [concept("c1", "Customer"), concept("c2", "Amount")];
      const page = options.page ?? 1;
      return { datasource_id: "ds", graph_version: "g1", total: 2, page, page_size: 100, items: [items[page - 1]] };
    }
    return { datasource_id: "ds", graph_version: "g1", total: 2, items: [column("a"), column("b")] };
  });
  api.detail.mockReset().mockResolvedValue({ datasource_id: "ds", graph_version: "g1", item: column("a"), mappings: [{ column: column("a").node, concept: concept("c1", "Customer").node, entity: null, field_to_concept_confidence: null, entity_to_concept_confidence: null }], page: 1, page_size: 100, total: 1 });
  api.save.mockReset().mockImplementation(async (_id, body) => ({ ...saved, changes: body.changes }));
  api.publish.mockReset().mockResolvedValue({ datasource_id: "ds", graph_version: "g2", previous_graph_version: "g1", draft_revision: 2, idempotency_key: "key" });
});
afterEach(() => { cleanup?.(); cleanup = undefined; vi.restoreAllMocks(); });

describe("DataLink draft editor", () => {
  it("loads an existing draft and publishes its exact revision", async () => {
    api.draft.mockResolvedValue(saved);
    const { published } = mount(); await settle();
    button("草稿与发布").click(); await settle();
    expect(document.body.textContent).toContain("禁用关系");
    button("发布修改").click(); await settle();
    expect(api.publish).toHaveBeenCalledWith("ds", { expected_head: "g1", expected_draft_revision: 2, idempotency_key: expect.any(String) }, expect.any(AbortSignal));
    expect(published).toHaveBeenCalledOnce();
  });

  it("stages a disabled manual candidate and requires saving before publishing", async () => {
    api.draft.mockResolvedValue({ ...saved, status: "published" });
    mount(); await settle(); button("草稿与发布").click(); await settle(); button("新增候选关系").click(); await settle();
    const source = document.querySelector<HTMLSelectElement>('[aria-label="起点字段"]')!;
    const target = document.querySelector<HTMLSelectElement>('[aria-label="终点字段"]')!;
    source.value = "a"; source.dispatchEvent(new Event("change", { bubbles: true }));
    target.value = "b"; target.dispatchEvent(new Event("change", { bubbles: true })); await settle();
    button("加入草稿").click(); await settle();
    expect(button("发布修改").disabled).toBe(true);
    button("保存草稿").click(); await settle();
    expect(api.save).toHaveBeenCalledWith("ds", expect.objectContaining({ schema_revision: 3, expected_draft_revision: 2, changes: [expect.objectContaining({ change_type: "add_relation", source_id: "a", target_id: "b", relation_type: "joinable", enabled: false })] }), expect.any(AbortSignal));
    expect(button("发布修改").disabled).toBe(false);
    expect(document.querySelector("button button")).toBeNull();
  });

  it("disables stale draft publication and ignores old datasource responses", async () => {
    let resolveOld!: (value: DataLinkDraft) => void;
    api.draft.mockImplementationOnce(() => new Promise<DataLinkDraft>((resolve) => { resolveOld = resolve; }));
    const { props } = mount();
    props.datasourceId = "ds2"; await settle(); resolveOld(saved); await settle();
    button("草稿与发布").click(); await settle();
    expect(document.body.textContent).toContain("暂无草稿修改");
    expect(button("发布修改").disabled).toBe(true);
  });

  it("preserves failed publication idempotency key on retry", async () => {
    api.draft.mockResolvedValue(saved); api.publish.mockRejectedValueOnce(new Error("connection lost"));
    mount(); await settle(); button("草稿与发布").click(); await settle();
    button("发布修改").click(); await settle();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("无法完成草稿操作");
    button("发布修改").click(); await settle();
    expect(api.publish.mock.calls[0][1].idempotency_key).toBe(api.publish.mock.calls[1][1].idempotency_key);
  });

  it("edits business text and aliases without renaming the physical column", async () => {
    const { editor } = mount(); await settle();
    editor.value!.editNode({ node: { id: "a", name: "amount", table: "orders", type: "column", description: "原说明", aliases: [], semantic_type: null, profile: null }, provenance: "structural", mapping_count: 0, relation_count: 0 }); await settle();
    const description = document.querySelector<HTMLTextAreaElement>('[aria-label="业务说明"]')!;
    description.value = "已扣优惠的实付金额"; description.dispatchEvent(new Event("input", { bubbles: true }));
    const aliases = document.querySelector<HTMLTextAreaElement>('[aria-label="别名"]')!;
    aliases.value = "实付\n成交额"; aliases.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    button("加入草稿").click(); await settle(); button("保存草稿").click(); await settle();
    expect(api.save.mock.calls[0][1].changes[0]).toEqual({ change_type: "update_node", object_key: "a", description: "已扣优惠的实付金额", aliases: ["实付", "成交额"], semantic_type: "" });
  });

  it("requires explicit enablement when correcting an enabled relation", async () => {
    const { editor } = mount(); await settle();
    const field = (id: string) => ({ id, name: id, table: "orders", type: "column" as const, description: null, aliases: [], semantic_type: null, profile: null });
    editor.value!.editRelation({ id: "edge1", source: field("a"), target: field("b"), type: "foreign_key", confidence: 1, evidence: null, provenance: "database_foreign_key", enabled: true, join_eligible: true }); await settle();
    const action = document.querySelector<HTMLSelectElement>('[aria-label="修订方式"]')!;
    action.value = "repoint"; action.dispatchEvent(new Event("change", { bubbles: true })); await settle();
    expect(document.querySelector<HTMLInputElement>('[aria-label="启用关系"]')!.checked).toBe(false);
    button("加入草稿").click(); await settle(); button("保存草稿").click(); await settle();
    expect(api.save.mock.calls[0][1].changes[0]).toMatchObject({ change_type: "repoint_relation", object_key: "edge1", relation_type: "joinable", enabled: false });
  });

  it("keeps node text and mapping changes independent and saves add_node first", async () => {
    const { editor } = mount(); await settle();
    editor.value!.editNode(column("a")); await settle();
    expect(document.body.textContent).toContain("自动说明");
    const description = document.querySelector<HTMLTextAreaElement>('[aria-label="业务说明"]')!;
    description.value = "订单原价"; description.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    button("加入草稿").click(); await settle();
    await editor.value!.editMapping(column("a")); await settle();
    expect(document.body.textContent).toContain("Customer");
    expect(document.body.textContent).toContain("Amount");
    const boxes = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
    expect(boxes.map((item) => item.value)).toEqual(["c1", "c2"]);
    boxes[1].click(); await settle();
    button("加入草稿").click(); await settle();
    button("新增属性").click(); await settle();
    const name = document.querySelector<HTMLInputElement>('[aria-label="新语义名称"]')!;
    name.value = "Revenue"; name.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    button("加入草稿").click(); await settle();
    button("保存草稿").click(); await settle();
    const changes = api.save.mock.calls[0][1].changes;
    expect(changes[0]).toMatchObject({ change_type: "add_node", name: "Revenue", node_type: "concept" });
    expect(changes.map((item: { change_type: string }) => item.change_type)).toEqual(["add_node", "update_node", "replace_mapping"]);
  });
  it("includes staged concepts in mapping choices and can restore automatic mapping", async () => {
    const { editor } = mount(); await settle();
    button("草稿与发布").click(); await settle();
    button("新增属性").click(); await settle();
    const name = document.querySelector<HTMLInputElement>('[aria-label="新语义名称"]')!;
    name.value = "Revenue"; name.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    button("加入草稿").click(); await settle();
    await editor.value!.editMapping(column("a")); await settle();
    expect(document.body.textContent).toContain("Revenue");
    expect(document.body.textContent).toContain("未保存新增");
    button("恢复自动映射").click(); await settle();
    button("保存草稿").click(); await settle();
    expect(api.save.mock.calls[0][1].changes.map((item: { change_type: string }) => item.change_type)).toEqual(["add_node", "reset_mapping"]);
  });
  it("retains unstaged text when leaving the form is canceled", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { editor } = mount(); await settle();
    editor.value!.editNode({ node: { id: "a", name: "amount", table: "orders", type: "column", description: null, aliases: [], semantic_type: null, profile: null }, provenance: "structural", mapping_count: 0, relation_count: 0 }); await settle();
    const input = document.querySelector<HTMLTextAreaElement>('[aria-label="业务说明"]')!;
    input.value = "未保存说明"; input.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    button("返回草稿").click(); await settle();
    expect(confirm).toHaveBeenCalledOnce();
    expect(editor.value!.hasUnsavedChanges).toBe(true);
    expect(document.querySelector<HTMLTextAreaElement>('[aria-label="业务说明"]')!.value).toBe("未保存说明");
  });
});
