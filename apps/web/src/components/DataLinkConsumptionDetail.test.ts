import { createApp, h, nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";
import type { DataLinkConsumption, DataLinkConsumptionList } from "@/api/types";
import DataLinkConsumptionDetail from "./DataLinkConsumptionDetail.vue";

function consumption(overrides: Partial<DataLinkConsumption> = {}): DataLinkConsumption {
  return {
    id: "cons_1",
    run_id: "run_1",
    stage: "prepare",
    seq: 1,
    query: "订单金额",
    focus: null,
    max_nodes: 12,
    schema_revision: 1,
    graph_version: "graph_1",
    mode: "live",
    payload_status: "complete",
    returned_status: "ok",
    consumer_receipt_status: "received",
    is_truncated: false,
    tool_call_id: null,
    payload_version: 1,
    semantic_context: {
      provider: "datalink",
      trust: "inferred",
      mode: "live",
      graph_version: "graph_1",
      semantic_catalog: { concepts: [], entities: [] },
      fields: [{ table: "orders", column: "amount", description: "未扣优惠", aliases: [], semantic_type: null, semantic_mappings: [] }],
      relationships: [],
      join_paths: [],
      warnings: [],
    },
    summary: { field_count: 1, relationship_count: 0, join_path_count: 0, warning_count: 0, payload_bytes: 120 },
    created_at: "2026-09-06T00:00:00Z",
    ...overrides,
  };
}

let cleanup: (() => void) | undefined;
afterEach(() => { cleanup?.(); cleanup = undefined; document.body.replaceChildren(); });

describe("DataLinkConsumptionDetail", () => {
  it("shows missing history without claiming the model adopted the payload", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const list: DataLinkConsumptionList = { run_id: "run_1", historical_status: "missing", items: [] };
    const app = createApp({ render: () => h(DataLinkConsumptionDetail, { open: true, list, selected: null }) });
    app.mount(host);
    cleanup = () => { app.unmount(); host.remove(); };
    await nextTick();
    expect(document.body.textContent).toContain("历史未记录完整内容");
    expect(document.body.textContent).toContain("不表示模型推理采用了这些内容");
  });

  it("renders a recorded prepare-stage payload", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const item = consumption();
    const list: DataLinkConsumptionList = { run_id: "run_1", historical_status: "recorded", items: [item] };
    const app = createApp({ render: () => h(DataLinkConsumptionDetail, { open: true, list, selected: item }) });
    app.mount(host);
    cleanup = () => { app.unmount(); host.remove(); };
    await nextTick();
    expect(document.body.textContent).toContain("准备阶段");
    expect(document.body.textContent).toContain("未扣优惠");
    expect(document.body.textContent).not.toContain("模型采用");
  });
});
