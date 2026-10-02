import { afterEach, describe, expect, it, vi } from "vitest";

import { createRun, getSession, listSessionMessages, updateSession } from "./sessions";
import {
  getDatasourceDatalinkEntries,
  getDatasourceDatalinkGraph,
  getDatasourceDatalinkSubgraph,
  uploadDatasource,
} from "./datasources";
import { activateModelProfile, listModelProfiles } from "./modelProfiles";
import { cancelRun, listRunArtifacts } from "./runs";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}

function envelope(data: unknown): Response {
  return Response.json({ data, request_id: "req_test", error: null });
}

describe("resource API clients", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("encodes session ids and sends the Run request body", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => envelope({ run_id: "run_1" }));
    vi.stubGlobal("fetch", fetchMock);

    await getSession("session/with space");
    await createRun("session/with space", { question: "销售额？", idempotency_key: "q-1" });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      apiUrl("/sessions/session%2Fwith%20space"),
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      apiUrl("/sessions/session%2Fwith%20space/runs"),
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      question: "销售额？",
      idempotency_key: "q-1",
    });
  });

  it("uses multipart FormData for datasource uploads", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => envelope({ id: "ds_1" }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["id,name\n1,Ada\n"], "people.csv", { type: "text/csv" });

    await uploadDatasource(file, "csv", "People", "Demo data");

    const request = fetchMock.mock.calls[0]?.[1];
    expect(request?.method).toBe("POST");
    expect(request?.body).toBeInstanceOf(FormData);
    expect(new Headers(request?.headers).has("content-type")).toBe(false);
    const form = request?.body as FormData;
    expect(form.get("type")).toBe("csv");
    expect(form.get("name")).toBe("People");
  });

  it("keeps pagination and datalink query parameters in resource paths", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => envelope({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await listModelProfiles(2, 10);
    await getDatasourceDatalinkGraph("ds_1", "graph/v2");
    await listRunArtifacts("run_1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      apiUrl("/model-profiles?page=2&page_size=10"),
      apiUrl("/datasources/ds_1/datalink/graph?graph_version=graph%2Fv2"),
      apiUrl("/runs/run_1/artifacts"),
    ]);
  });

  it("encodes data-map entry and repeated edge filters in resource paths", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => envelope({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await getDatasourceDatalinkEntries("ds/1", {
      graphVersion: "graph/v2",
      entryType: "table",
      query: "订单 表",
      page: 2,
      pageSize: 100,
    });
    await getDatasourceDatalinkSubgraph("ds/1", "table/orders", {
      graphVersion: "graph/v2",
      edgeTypes: ["foreign_key", "joinable"],
      hops: 2,
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      apiUrl("/datasources/ds%2F1/datalink/entries?graph_version=graph%2Fv2&entry_type=table&query=%E8%AE%A2%E5%8D%95+%E8%A1%A8&page=2&page_size=100"),
      apiUrl("/datasources/ds%2F1/datalink/subgraph?root_node_id=table%2Forders&graph_version=graph%2Fv2&hops=2&edge_types=foreign_key&edge_types=joinable"),
    ]);
  });

  it("uses PATCH and POST action endpoints for updates and cancellation", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => envelope({ id: "x" }));
    vi.stubGlobal("fetch", fetchMock);

    await updateSession("session_1", { title: "新标题" });
    await activateModelProfile("profile_1");
    await cancelRun("run_1", { reason: "user_requested" });
    await listSessionMessages("session_1", 4, 3);

    expect(fetchMock.mock.calls.map(([, init]) => init?.method ?? "GET")).toEqual([
      "PATCH",
      "POST",
      "POST",
      "GET",
    ]);
    expect(fetchMock.mock.calls[3]?.[0]).toBe(
      apiUrl("/sessions/session_1/messages?page_size=4&before_position=3"),
    );
  });
});
