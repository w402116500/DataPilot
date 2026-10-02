import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import type { DataLinkGraph, DataLinkStatus, DataSource, SchemaSummary, TableData } from "@/api/types";

const api = vi.hoisted(() => ({
  deleteDatasource: vi.fn(),
  getDatasourceDatalinkGraph: vi.fn(),
  getDatasourceDatalinkStatus: vi.fn(),
  getDatasourceSchema: vi.fn(),
  listDatasources: vi.fn(),
  previewDatasourceTable: vi.fn(),
  rebuildDatasourceDatalink: vi.fn(),
  retryDatasource: vi.fn(),
  updateDatasourceDescription: vi.fn(),
  updateDatasourceMaskFields: vi.fn(),
  uploadDatasource: vi.fn(),
}));

vi.mock("@/api/datasources", () => api);

import { useDatasourceStore } from "./datasourceStore";

function datasource(status: DataSource["status"] = "ready"): DataSource {
  return {
    id: "datasource_1",
    name: "销售订单",
    description: null,
    type: "csv",
    status,
    schema_revision: 2,
    mask_fields: [],
    mask_fields_confirmed: false,
    schema: null,
    datalink_build_id: "build_1",
    datalink_graph_version: "graph_2",
    last_error_code: null,
    last_error_message: null,
    last_test_at: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
  };
}

describe("datasourceStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it("projects uploaded datasource, schema, preview and graph data by datasource id", async () => {
    const uploaded = datasource("inspecting");
    const schema: SchemaSummary = {
      datasource_id: uploaded.id,
      dialect: "sqlite",
      tables: [],
    };
    const preview: TableData = {
      columns: ["amount"],
      rows: [[100]],
      row_count: 1,
    };
    const graph: DataLinkGraph = {
      datasource_id: uploaded.id,
      graph_version: "graph_2",
      nodes: [],
      edges: [],
      warnings: [],
    };
    const datalinkStatus: DataLinkStatus = {
      datasource_id: uploaded.id,
      current_graph_version: "graph_2",
      current_build: null,
      last_error_code: null,
      last_error_message: null,
    };
    api.uploadDatasource.mockResolvedValue(uploaded);
    api.getDatasourceSchema.mockResolvedValue(schema);
    api.previewDatasourceTable.mockResolvedValue(preview);
    api.getDatasourceDatalinkGraph.mockResolvedValue(graph);
    api.getDatasourceDatalinkStatus.mockResolvedValue(datalinkStatus);
    const store = useDatasourceStore();
    const file = new File(["amount\n100\n"], "orders.csv", { type: "text/csv" });

    await store.upload(file, "csv", "销售订单");
    await store.loadSchema(uploaded.id);
    await store.preview(uploaded.id, "orders");
    await store.loadDatalinkGraph(uploaded.id, "graph_2");
    await store.loadDatalinkStatus(uploaded.id);

    expect(api.uploadDatasource).toHaveBeenCalledWith(file, "csv", "销售订单", undefined);
    expect(store.items).toEqual([uploaded]);
    expect(store.schemas[uploaded.id]).toEqual(schema);
    expect(store.previews[`${uploaded.id}:orders`]).toEqual(preview);
    expect(store.datalinkGraphs[uploaded.id]).toEqual(graph);
    expect(store.datalinkStatuses[uploaded.id]).toEqual(datalinkStatus);
  });

  it("keeps the deleted datasource as a tombstone returned by the server", async () => {
    const store = useDatasourceStore();
    store.items = [datasource()];
    api.deleteDatasource.mockResolvedValue({ datasource_id: "datasource_1", status: "deleted" });

    await store.remove("datasource_1");

    expect(store.items).toEqual([datasource("deleted")]);
  });

  it("replaces the datasource after the owner confirms its mask fields", async () => {
    const store = useDatasourceStore();
    store.items = [datasource("schema_ready")];
    const confirmed = {
      ...datasource("schema_ready"),
      mask_fields: ["customer_email"],
      mask_fields_confirmed: true,
    };
    api.updateDatasourceMaskFields.mockResolvedValue(confirmed);

    await store.updateMaskFields("datasource_1", ["customer_email"]);

    expect(api.updateDatasourceMaskFields).toHaveBeenCalledWith("datasource_1", ["customer_email"]);
    expect(store.items).toEqual([confirmed]);
  });

  it("replaces the datasource after the owner updates its description", async () => {
    const store = useDatasourceStore();
    store.items = [datasource("schema_ready")];
    const updated = { ...datasource("schema_ready"), description: "电商订单主表" };
    api.updateDatasourceDescription.mockResolvedValue(updated);

    await store.updateDescription("datasource_1", "电商订单主表");

    expect(api.updateDatasourceDescription).toHaveBeenCalledWith("datasource_1", "电商订单主表");
    expect(store.items).toEqual([updated]);
  });
});
