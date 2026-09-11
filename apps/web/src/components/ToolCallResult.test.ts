import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";

import type { ToolCall } from "@/api/types";

import ToolCallResult from "./ToolCallResult.vue";

function tool(overrides: Partial<ToolCall>): ToolCall {
  return {
    id: "tool_1",
    run_id: "run_1",
    tool_name: "run_python",
    status: "succeeded",
    input_params: null,
    output_summary: null,
    error_code: null,
    error_message: null,
    started_at: "2026-08-16T00:00:00Z",
    finished_at: "2026-08-16T00:00:01Z",
    ...overrides,
  };
}

async function mountToolCall(value: ToolCall): Promise<{ host: HTMLElement; unmount: () => void }> {
  const app = createApp(ToolCallResult, { tool: value });
  const host = document.createElement("div");
  document.body.append(host);
  app.mount(host);
  await nextTick();
  return { host, unmount: () => app.unmount() };
}

describe("ToolCallResult", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("按 Python 语义展示目的、脚本、声明产物、退出码和 stdout", async () => {
    const mounted = await mountToolCall(tool({
      input_params: {
        purpose: "生成城市汇总",
        script: "print('done')",
        output_paths: ["city_summary.json"],
      },
      output_summary: { exit_code: 0, stdout: "done" },
    }));

    expect(mounted.host.textContent).toContain("Python 分析");
    expect(mounted.host.textContent).toContain("生成城市汇总");
    expect(mounted.host.textContent).toContain("city_summary.json");
    expect(mounted.host.textContent).toContain("print('done')");
    expect(mounted.host.textContent).toContain("退出码");
    expect(mounted.host.textContent).toContain("标准输出");
    mounted.unmount();
  });

  it("按 DataLink 语义展示 query、focus 和节点上限", async () => {
    const mounted = await mountToolCall(tool({
      tool_name: "explore_datalink",
      input_params: { query: "城市和订单如何关联", focus: "city", max_nodes: 30 },
      output_summary: {
        graph_version: "graph-v2",
        node_count: 4,
        edge_count: 3,
        semantic_entity_count: 1,
        semantic_mapping_count: 4,
        semantic_entities: "loan_application",
      },
    }));

    expect(mounted.host.textContent).toContain("关系探索条件");
    expect(mounted.host.textContent).toContain("城市和订单如何关联");
    expect(mounted.host.textContent).toContain("city");
    expect(mounted.host.textContent).toContain("30");
    expect(mounted.host.textContent).toContain("graph-v2");
    expect(mounted.host.textContent).toContain("语义实体");
    expect(mounted.host.textContent).toContain("loan_application");
    mounted.unmount();
  });

  it("按工具类型和运行状态输出独立的视觉语义 class", async () => {
    const sql = await mountToolCall(tool({ tool_name: "run_sql_readonly", status: "running" }));
    const python = await mountToolCall(tool({ tool_name: "run_python" }));
    const datalink = await mountToolCall(tool({ tool_name: "explore_datalink" }));

    expect(sql.host.querySelector(".tool-call-result.tool-sql")).not.toBeNull();
    expect(sql.host.querySelector(".tool-call-status.status-running")).not.toBeNull();
    expect(python.host.querySelector(".tool-call-result.tool-python")).not.toBeNull();
    expect(datalink.host.querySelector(".tool-call-result.tool-datalink")).not.toBeNull();
    sql.unmount();
    python.unmount();
    datalink.unmount();
  });

  it("不把内部 audit/artifact 标识展示给用户", async () => {
    const mounted = await mountToolCall(tool({
      tool_name: "run_sql_readonly",
      output_summary: {
        row_count: 48,
        audit_log_id: "audit_internal",
        artifact_id: "artifact_internal",
        execution_status: "succeeded",
      },
    }));

    expect(mounted.host.textContent).toContain("返回行数");
    expect(mounted.host.textContent).toContain("48");
    expect(mounted.host.textContent).not.toContain("audit_internal");
    expect(mounted.host.textContent).not.toContain("artifact_internal");
    expect(mounted.host.textContent).not.toContain("audit_log_id");
    mounted.unmount();
  });

  it("嵌入过程卡时不再重复工具名和详情折叠开关", async () => {
    const app = createApp(ToolCallResult, {
      tool: tool({
        tool_name: "run_sql_readonly",
        input_params: { sql: "SELECT 1" },
      }),
      embedded: true,
    });
    const host = document.createElement("div");
    document.body.append(host);
    app.mount(host);
    await nextTick();

    expect(host.querySelector(".tool-call-result.is-embedded")).not.toBeNull();
    expect(host.querySelector(".tool-call-heading")).toBeNull();
    expect(host.querySelector(".tool-result-toggle")).toBeNull();
    expect(host.textContent).toContain("SELECT 1");
    expect(host.textContent).not.toContain("查看本次调用详情");
    app.unmount();
  });

  it("优先展示服务端保存的人话失败原因", async () => {
    const mounted = await mountToolCall(tool({
      tool_name: "run_sql_readonly",
      status: "failed",
      error_code: "SCHEMA_REDUNDANT_METADATA_QUERY",
      error_message: "当前 Run 已提供数据结构，系统元数据查询不会产生新的业务证据。",
    }));

    expect(mounted.host.textContent).toContain("当前 Run 已提供数据结构");
    expect(mounted.host.textContent).not.toContain("调用未完成：SCHEMA_REDUNDANT_METADATA_QUERY");
    mounted.unmount();
  });
});
