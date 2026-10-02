import { createApp, h, nextTick, reactive } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DataLinkCatalog, DataLinkCatalogRelation, DataLinkEdgeType, DataLinkPreview } from "@/api/types";
import DataLinkWorkspace from "./DataLinkWorkspace.vue";

const api = vi.hoisted(() => ({ catalog: vi.fn(), detail: vi.fn(), relations: vi.fn(), preview: vi.fn(), draft: vi.fn(), save: vi.fn(), publish: vi.fn(), validations: vi.fn(), createValidation: vi.fn(), cancelValidation: vi.fn() }));
vi.mock("@/api/datasources", () => ({ getDatalinkCatalog: api.catalog, getDatalinkCatalogDetail: api.detail, getDatalinkRelations: api.relations, previewDatalink: api.preview, getDatalinkDraft: api.draft, saveDatalinkDraft: api.save, publishDatalinkDraft: api.publish, listDatalinkValidations: api.validations, createDatalinkValidation: api.createValidation, cancelDatalinkValidation: api.cancelValidation }));

function catalogItem(
  name: string,
  table: string,
  description: string | null = "订单原价",
  primaryMapping: DataLinkCatalog["items"][number]["primary_mapping"] = null,
): DataLinkCatalog["items"][number] {
  return {
    node: { id: `${table}.${name}`, name, type: "column", table, description, aliases: [], semantic_type: null, profile: null },
    provenance: "structural", mapping_count: 1, relation_count: 2,
    primary_mapping: primaryMapping,
  };
}

function catalog(name: string, source = "ds_1"): DataLinkCatalog {
  return { datasource_id: source, graph_version: "graph_1", page: 1, page_size: 20, total: 1, items: [catalogItem(name, "orders")] };
}

function conceptCatalog(): DataLinkCatalog {
  return {
    datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 1,
    items: [{
      node: { id: "concept.customer", name: "客户", type: "concept", table: null, description: "下单客户", aliases: ["buyer"], semantic_type: null, profile: null },
      provenance: "manual", mapping_count: 3, relation_count: 0,
      mapped_columns: [
        { table: "customers", name: "customer_id" },
        { table: "events", name: "customer_id" },
        { table: "orders", name: "customer_id" },
      ],
    }],
  };
}

const result: DataLinkPreview = {
  datasource_id: "ds_1", schema_revision: 1, graph_version: "graph_1", query: "订单金额", focus: null, max_nodes: 12,
  retrieval_mode: "keyword", is_truncated: false,
  semantic_context: { provider: "datalink", trust: "inferred", mode: "live", graph_version: "graph_1", semantic_catalog: { concepts: [], entities: [] },
    fields: [{ table: "orders", column: "amount", description: "未扣优惠", aliases: [], semantic_type: null, semantic_mappings: [] }], relationships: [], join_paths: [], warnings: [] },
};

let cleanup: (() => void) | undefined;
async function settle(): Promise<void> {
  for (let step = 0; step < 6; step += 1) {
    await Promise.resolve();
    await nextTick();
  }
}
function mount() {
  const props = reactive({ datasourceId: "ds_1", graphVersion: "graph_1", schemaRevision: 1 });
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp({ render: () => h(DataLinkWorkspace, props) }); app.mount(host);
  cleanup = () => { app.unmount(); host.remove(); };
  return { host, props };
}
function click(host: HTMLElement, text: string): void {
  const button = Array.from(host.querySelectorAll("button")).find((element) => element.textContent?.includes(text));
  expect(button).toBeDefined(); button!.click();
}

beforeEach(() => {
  api.draft.mockReset().mockResolvedValue(null);
  api.catalog.mockReset().mockResolvedValue(catalog("amount"));
  api.detail.mockReset().mockResolvedValue({ datasource_id: "ds_1", graph_version: "graph_1", item: catalog("amount").items[0], mappings: [], page: 1, page_size: 20, total: 0 });
  api.relations.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20, graph_version: "graph_1", datasource_id: "ds_1" });
  api.preview.mockReset().mockResolvedValue(result);
  api.validations.mockReset().mockResolvedValue([]);
  api.createValidation.mockReset();
  api.cancelValidation.mockReset();
});
afterEach(() => { cleanup?.(); cleanup = undefined; });

describe("DataLink workspace", () => {
  it("loads a paginated physical field catalog without reconstructing mappings", async () => {
    const { host } = mount(); await settle();
    expect(api.catalog).toHaveBeenCalledWith("ds_1", expect.objectContaining({ graphVersion: "graph_1", type: "column", page: 1, pageSize: 100 }), expect.any(AbortSignal));
    expect(host.textContent).toContain("字段说明书");
    expect(host.textContent).toContain("amount");
    expect(host.textContent).toContain("订单原价");
    expect(host.textContent).not.toContain("1 映射");
    expect(host.querySelector("#datalink-tab-preview")).toBeNull();
  });

  it("keeps a table on one field page instead of splitting it at 20 rows", async () => {
    const items = [
      ...["channel", "country", "customer_id", "signup_date"].map((name) => catalogItem(name, "customers")),
      ...["customer_id", "event_id", "event_ts", "event_type", "session_id"].map((name) => catalogItem(name, "events")),
      ...["order_id", "order_item_id", "product_id", "quantity", "unit_price"].map((name) => catalogItem(name, "order_items")),
      ...["customer_id", "order_id", "order_ts", "status"].map((name) => catalogItem(name, "orders")),
      ...["category", "product_id", "product_name", "unit_cost", "unit_price"].map((name) => catalogItem(name, "products")),
    ];
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 100, total: items.length, items,
    });
    const { host } = mount(); await settle();
    expect(host.textContent).toContain("customers");
    expect(host.textContent).toContain("orders");
    expect(host.textContent).not.toContain("products");
    expect(host.textContent).toMatch(/23 项/);
    expect(host.textContent).toMatch(/1 \/ 2/);
    const next = host.querySelector<HTMLButtonElement>('button[aria-label="下一页"]');
    expect(next).not.toBeNull();
    next!.click(); await settle();
    expect(api.catalog).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("products");
    expect(host.textContent).toContain("product_name");
    expect(host.textContent).toContain("unit_cost");
    expect(host.textContent).not.toContain("customers");
    expect(host.textContent).toMatch(/2 \/ 2/);
  });

  it("groups same-named columns by table and opens the existing node editor", async () => {
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 2,
      items: [catalogItem("customer_id", "customers", "客户主键"), catalogItem("customer_id", "orders", "订单客户")],
    });
    const { host } = mount(); await settle();
    expect(host.textContent).toContain("customers");
    expect(host.textContent).toContain("orders");
    expect(host.textContent).toMatch(/customer_id[\s\S]*customer_id/);
    click(host, "快速编辑"); await settle();
    expect(document.body.textContent).toContain("编辑业务语义");
    expect(document.body.textContent).toContain("customers.customer_id");
  });

  it("shows a mapped concept when the field has no node description", async () => {
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 1,
      items: [catalogItem("customer_id", "customers", null, {
        concept_name: "客户标识", concept_description: "下单客户主键", entity_name: "客户",
        field_to_concept_confidence: 0.9, provenance: "unknown",
      })],
    });
    const { host } = mount(); await settle();
    expect(host.textContent).toContain("客户标识");
    expect(host.textContent).toContain("下单客户主键");
    expect(host.textContent).toContain("客户");
    expect(host.textContent).not.toContain("来自概念");
    expect(host.textContent).not.toContain("业务说明未记录");
    expect(host.textContent).not.toContain("未映射业务概念");
    click(host, "快速编辑"); await settle();
    expect(document.body.textContent).toContain("列表上的映射说明不会写入字段，除非你在此保存");
    const field = document.body.querySelector<HTMLTextAreaElement>('textarea[aria-label="业务说明"]');
    expect(field).not.toBeNull();
    expect(field!.value).toBe("");
  });

  it("keeps a written field description ahead of the mapped concept", async () => {
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 1,
      items: [catalogItem("channel", "customers", "验收覆盖说明", {
        concept_name: "获客渠道", concept_description: "流量来源", entity_name: "客户",
        field_to_concept_confidence: 0.8, provenance: "manual",
      })],
    });
    const { host } = mount(); await settle();
    expect(host.textContent).toContain("获客渠道");
    expect(host.textContent).toContain("流量来源");
    expect(host.textContent).toContain("客户");
    expect(host.textContent).toContain("手写说明");
    expect(host.textContent).not.toContain("来自概念");
    expect(host.textContent).not.toContain("验收覆盖说明");
  });

  it("labels columns that have no enabled mapping", async () => {
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 1,
      items: [catalogItem("notes", "orders", null)],
    });
    const { host } = mount(); await settle();
    expect(host.textContent).toContain("未映射");
    expect(host.textContent).not.toContain("业务说明未记录");
  });

  it("loads business concepts as a dedicated catalog view", async () => {
    api.catalog.mockImplementation((_id: string, options: { type?: string }) =>
      Promise.resolve(options.type === "concept" ? conceptCatalog() : catalog("amount")),
    );
    const { host } = mount(); await settle();
    click(host, "业务属性"); await settle();
    expect(api.catalog).toHaveBeenCalledWith("ds_1", expect.objectContaining({ type: "concept" }), expect.any(AbortSignal));
    expect(host.textContent).toContain("客户");
    expect(host.textContent).toContain("下单客户");
    expect(host.textContent).toContain("customers.customer_id");
    expect(host.textContent).toContain("events.customer_id");
    expect(host.textContent).toContain("orders.customer_id");
    expect(host.textContent).not.toContain("3 映射");
  });

  it("collapses cartesian mappings onto one primary path with extra entities", async () => {
    const columnNode = {
      id: "customers.customer_id", name: "customer_id", type: "column" as const, table: "customers",
      description: null, aliases: [] as string[], semantic_type: null, profile: null,
    };
    const conceptNode = {
      id: "concept.customer_id", name: "客户标识", type: "concept" as const, table: null,
      description: "客户主键", aliases: [] as string[], semantic_type: null, profile: null,
    };
    const entityNode = (id: string, name: string) => ({
      id, name, type: "entity" as const, table: null, description: null, aliases: [] as string[], semantic_type: null, profile: null,
    });
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 1,
      items: [catalogItem("customer_id", "customers", null, {
        concept_name: "客户标识", concept_description: "客户主键", entity_name: "客户",
        field_to_concept_confidence: 0.95, provenance: "unknown",
      })],
    });
    api.detail.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 3,
      item: catalogItem("customer_id", "customers", null, {
        concept_name: "客户标识", concept_description: "客户主键", entity_name: "客户",
        field_to_concept_confidence: 0.95, provenance: "unknown",
      }),
      mappings: [
        { column: columnNode, concept: conceptNode, entity: entityNode("e1", "客户"), field_to_concept_confidence: 0.95, entity_to_concept_confidence: 0.95, enabled: true },
        { column: columnNode, concept: conceptNode, entity: entityNode("e2", "用户事件记录"), field_to_concept_confidence: 0.95, entity_to_concept_confidence: 0.9, enabled: true },
        { column: columnNode, concept: conceptNode, entity: entityNode("e3", "订单"), field_to_concept_confidence: 0.95, entity_to_concept_confidence: 0.95, enabled: true },
      ],
    });
    const { host } = mount(); await settle();
    host.querySelector<HTMLButtonElement>(".catalog-row-main")!.click(); await settle();
    const text = document.body.textContent ?? "";
    expect(text).toContain("客户标识");
    expect(text).toContain("客户");
    expect(text).toContain("其他归属：订单、用户事件记录");
    expect(text.split("customers.customer_id").length - 1).toBe(1);
  });

  it("rejects a late response after changing datasource", async () => {
    let resolveOld!: (value: DataLinkCatalog) => void;
    api.catalog.mockImplementationOnce(() => new Promise<DataLinkCatalog>((resolve) => { resolveOld = resolve; }));
    const { host, props } = mount();
    api.catalog.mockResolvedValue(catalog("new_field", "ds_2"));
    props.datasourceId = "ds_2"; await settle();
    resolveOld(catalog("stale_field")); await settle();
    expect(host.textContent).toContain("new_field");
    expect(host.textContent).not.toContain("stale_field");
  });

  it("groups field relations by table with catalog attributes and omits mapping edges", async () => {
    function node(name: string, type: "column" | "concept", table: string | null): DataLinkCatalogRelation["source"] {
      return { id: table ? `${table}.${name}` : `concept:${name}`, name, type, table, description: null, aliases: [], semantic_type: null, profile: null };
    }
    function edge(id: string, type: DataLinkEdgeType, source: DataLinkCatalogRelation["source"], target: DataLinkCatalogRelation["target"], extras: Partial<DataLinkCatalogRelation> = {}): DataLinkCatalogRelation {
      return { id, enabled: true, join_eligible: false, type, confidence: 1, provenance: "semantic_mapping", evidence: null, source, target, ...extras };
    }
    function mapped(name: string, table: string, concept: string) {
      return catalogItem(name, table, null, {
        concept_name: concept, concept_description: null, entity_name: "客户",
        field_to_concept_confidence: 0.9, provenance: "unknown",
      });
    }
    const items = [
      ...Array.from({ length: 12 }, (_, index) => edge(
        `rep_${index}`,
        "represents",
        node(`c${index}`, "column", "customers"),
        node(`attr_${index}`, "concept", null),
      )),
      ...Array.from({ length: 8 }, (_, index) => edge(
        `syn_${index}`,
        "semantic_synonym",
        node("customer_id", "column", "customers"),
        node(`e${index}`, "column", "events"),
      )),
      ...Array.from({ length: 8 }, (_, index) => edge(
        `join_${index}`,
        "joinable",
        node("customer_id", "column", "orders"),
        node(`id_${index}`, "column", "customers"),
        { join_eligible: true },
      )),
    ];
    api.relations.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 100, total: items.length, items,
    });
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 100, total: 18,
      items: [
        mapped("customer_id", "customers", "客户标识"),
        ...Array.from({ length: 8 }, (_, index) => mapped(`id_${index}`, "customers", "客户标识")),
        ...Array.from({ length: 8 }, (_, index) => mapped(`e${index}`, "events", "事件字段")),
        mapped("customer_id", "orders", "客户标识"),
      ],
    });
    const { host } = mount(); await settle();
    host.querySelector<HTMLButtonElement>("#datalink-tab-relations")!.click(); await settle();
    expect(api.relations).toHaveBeenCalledWith("ds_1", expect.objectContaining({ graphVersion: "graph_1", page: 1, pageSize: 100 }), expect.any(AbortSignal));
    expect(api.catalog).toHaveBeenCalledWith("ds_1", expect.objectContaining({ type: "column", page: 1, pageSize: 100 }), expect.any(AbortSignal));
    expect(host.querySelector('select[aria-label="关系种类"]')).toBeNull();
    expect(host.textContent).toContain("客户标识");
    expect(host.textContent).toContain("候选关联");
    expect(host.textContent).toContain("可用于 Join 候选");
    expect(host.textContent).toContain("语义同义");
    expect(host.textContent).not.toContain("字段映射");
    expect(host.textContent).not.toContain("结构 / 语义说明");
    expect(host.textContent).toMatch(/16 项/);
    expect(host.textContent).toMatch(/1 \/ 2/);
    expect(Array.from(host.querySelectorAll(".field-group h3")).map((element) => element.textContent)).toEqual(["customers"]);
    host.querySelector<HTMLButtonElement>('button[aria-label="下一页"]')!.click(); await settle();
    expect(api.relations).toHaveBeenCalledTimes(1);
    expect(Array.from(host.querySelectorAll(".field-group h3")).map((element) => element.textContent)).toEqual(["events", "orders"]);
    expect(host.textContent).toContain("事件字段");
    expect(host.textContent).not.toContain("字段映射");
    expect(host.textContent).toMatch(/2 \/ 2/);
  });

  it("fails the relation list when the column catalog cannot be loaded", async () => {
    api.relations.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 100, total: 1,
      items: [{
        id: "rel_1", enabled: true, join_eligible: true, type: "foreign_key", confidence: 1,
        provenance: "database_foreign_key", evidence: { kind: "fk", summary: "declared" },
        source: { id: "orders.customer_id", name: "customer_id", type: "column", table: "orders", description: null, aliases: [], semantic_type: null, profile: null },
        target: { id: "customers.id", name: "id", type: "column", table: "customers", description: null, aliases: [], semantic_type: null, profile: null },
      }],
    });
    const { host } = mount(); await settle();
    api.catalog.mockRejectedValueOnce(new Error("boom"));
    host.querySelector<HTMLButtonElement>("#datalink-tab-relations")!.click(); await settle();
    expect(host.textContent).toContain("暂时无法读取 DataLink");
    expect(host.querySelector(".catalog-list")).toBeNull();
  });

  it("can start relation validation from the relation detail dialog", async () => {
    api.relations.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 20, total: 1,
      items: [{
        id: "rel_1", enabled: true, join_eligible: true, type: "foreign_key", confidence: 1,
        provenance: "database_foreign_key", evidence: { kind: "fk", summary: "declared" },
        source: { id: "orders.customer_id", name: "customer_id", type: "column", table: "orders", description: null, aliases: [], semantic_type: null, profile: null },
        target: { id: "customers.id", name: "id", type: "column", table: "customers", description: null, aliases: [], semantic_type: null, profile: null },
      }],
    });
    api.catalog.mockResolvedValue({
      datasource_id: "ds_1", graph_version: "graph_1", page: 1, page_size: 100, total: 2,
      items: [
        catalogItem("id", "customers", null, {
          concept_name: "客户标识", concept_description: "下单客户", entity_name: "客户",
          field_to_concept_confidence: 0.9, provenance: "unknown",
        }),
        catalogItem("customer_id", "orders", null, {
          concept_name: "客户标识", concept_description: "下单客户", entity_name: "客户",
          field_to_concept_confidence: 0.9, provenance: "unknown",
        }),
      ],
    });
    api.createValidation.mockResolvedValue({
      id: "val_1", datasource_id: "ds_1", relation_id: "rel_1", graph_version: "graph_1", schema_revision: 1,
      status: "completed", source_non_null_count: 3, target_non_null_count: 3, source_distinct_count: 3,
      target_distinct_count: 2, target_duplicate_count: 1, source_unmatched_count: 1, multiple_match_risk: true,
      direction: "source_to_target", endpoint_fingerprint: "fp", audit_log_ids: ["audit_1"], artifact_ids: ["art_1"],
      error_code: null, expired: false, created_at: "2026-09-06T00:00:00Z", finished_at: "2026-09-06T00:01:00Z",
    });
    const { host } = mount(); await settle();
    host.querySelector<HTMLButtonElement>("#datalink-tab-relations")!.click(); await settle();
    host.querySelector<HTMLButtonElement>(".relation-row-main")!.click(); await settle();
    const detail = document.body.querySelector(".datalink-detail");
    expect(detail).not.toBeNull();
    expect(detail?.textContent).toContain("customers.id");
    expect(detail?.textContent).toContain("字段");
    expect(detail?.textContent).toContain("对端字段");
    expect(detail?.textContent).toContain("orders.customer_id");
    expect(detail?.textContent).toContain("客户标识");
    expect(detail?.textContent).toContain("客户");
    expect(detail?.textContent).toContain("可用于 Join 候选");
    expect(detail?.textContent).toContain("数据库外键声明");
    expect(detail?.textContent).not.toContain("起点");
    expect(detail?.textContent).not.toContain("终点");
    expect(detail?.textContent).not.toContain("结构或语义说明");
    expect(detail?.textContent).not.toContain("可连接候选");
    expect(detail?.textContent).not.toContain("关系详情");
    Array.from(document.body.querySelectorAll("button")).find((element) => element.textContent?.includes("核验关系"))!.click(); await settle();
    expect(api.createValidation).toHaveBeenCalledWith("ds_1", expect.objectContaining({ relation_id: "rel_1", graph_version: "graph_1", schema_revision: 1 }));
    expect(document.body.textContent).toContain("已完成");
  });

  it("uses the preview endpoint and discards results when the question changes", async () => {
    const { host } = mount(); await settle(); click(host, "载荷预览"); await settle();
    const input = document.body.querySelector("textarea[aria-label='检索问题']") as HTMLTextAreaElement;
    expect(input).toBeTruthy();
    input.value = "订单金额"; input.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    document.body.querySelector(".preview-form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); await settle();
    expect(api.preview).toHaveBeenCalledWith("ds_1", { graph_version: "graph_1", query: "订单金额", focus: null, max_nodes: 12 }, expect.any(AbortSignal));
    expect(document.body.textContent).toContain("未扣优惠");
    expect(document.body.textContent).toContain("核对清单");
    input.value = "客户金额"; input.dispatchEvent(new Event("input", { bubbles: true })); await settle();
    expect(document.body.textContent).not.toContain("未扣优惠");
    expect(document.body.textContent).toContain("尚无检索结果");
  });
});
