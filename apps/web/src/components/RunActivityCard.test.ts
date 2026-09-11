import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";

import type { RunActivity } from "@/lib/runActivity";

import RunActivityCard from "./RunActivityCard.vue";

function activity(overrides: Partial<RunActivity> = {}): RunActivity {
  return {
    runId: "run_1",
    status: "succeeded",
    completionKind: "completed",
    summary: "分析完成",
    items: [{
      id: "tool:tool_1",
      runId: "run_1",
      seq: 2,
      kind: "tool",
      status: "succeeded",
      title: "查询数据",
      detail: "完成，用时 8 ms",
      target: "trace",
      toolCallId: "tool_1",
      artifactId: null,
    }],
    steps: [{
      id: "step:run_1:1",
      runId: "run_1",
      seq: 2,
      title: "第 1 步 · 查询数据",
      detail: "完成 1 个工具，用时 8 ms",
      goal: "确认 orders 的查询结果",
      action: "查询数据",
      result: "返回 12 行",
      status: "succeeded",
      turnNo: 1,
      toolCallIds: ["tool_1"],
      items: [{
        id: "tool:tool_1",
        runId: "run_1",
        seq: 2,
        kind: "tool",
        status: "succeeded",
        title: "查询数据",
        detail: "完成，用时 8 ms",
        target: "trace",
        toolCallId: "tool_1",
        artifactId: null,
      }],
    }],
    displayEntries: [{
      kind: "step",
      id: "step:run_1:1",
      seq: 2,
      stepNumber: 1,
      step: {
        id: "step:run_1:1",
        runId: "run_1",
        seq: 2,
        title: "第 1 步 · 查询数据",
        detail: "完成 1 个工具，用时 8 ms",
        goal: "确认 orders 的查询结果",
        action: "查询数据",
        result: "返回 12 行",
        status: "succeeded",
        turnNo: 1,
        toolCallIds: ["tool_1"],
        items: [{
          id: "tool:tool_1",
          runId: "run_1",
          seq: 2,
          kind: "tool",
          status: "succeeded",
          title: "查询数据",
          detail: "完成，用时 8 ms",
          target: "trace",
          toolCallId: "tool_1",
          artifactId: null,
        }],
      },
    }],
    toolCount: 1,
    artifactCount: 0,
    auditCount: 0,
    ...overrides,
  };
}

async function mountActivity(value: RunActivity): Promise<{ host: HTMLElement; unmount: () => void }> {
  const app = createApp(RunActivityCard, { activity: value });
  const host = document.createElement("div");
  document.body.append(host);
  app.mount(host);
  await nextTick();
  return { host, unmount: () => app.unmount() };
}

describe("RunActivityCard", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("完整成功后默认收起过程，用户展开后成功步骤仍保持紧凑", async () => {
    const mounted = await mountActivity(activity());
    expect(mounted.host.querySelector(".activity-list")).toBeNull();

    mounted.host.querySelector<HTMLButtonElement>(".activity-summary")?.click();
    await nextTick();
    expect(mounted.host.querySelector(".activity-list")).not.toBeNull();
    expect(mounted.host.querySelector(".activity-tool-list")).toBeNull();
    expect(mounted.host.textContent).toContain("查看 1 个调用");
    expect(mounted.host.textContent).toContain("目标确认 orders 的查询结果");
    expect(mounted.host.textContent).toContain("动作查询数据");
    expect(mounted.host.textContent).toContain("结果返回 12 行");
    mounted.unmount();
  });

  it("按 displayEntries 的 seq 顺序交错渲染准备阶段和工具步骤", async () => {
    const preparation = {
      id: "preparation:requirements",
      runId: "run_1",
      seq: 1,
      kind: "analysis" as const,
      status: "succeeded" as const,
      title: "整理分析目标",
      detail: "已完成，用时 12 ms",
      target: "overview" as const,
      toolCallId: null,
      artifactId: null,
    };
    const mounted = await mountActivity(activity({
      items: [preparation, ...activity().items],
      displayEntries: [
        { kind: "item", id: preparation.id, seq: preparation.seq, item: preparation },
        activity().displayEntries[0]!,
      ],
    }));

    mounted.host.querySelector<HTMLButtonElement>(".activity-summary")?.click();
    await nextTick();
    const rows = [...mounted.host.querySelectorAll<HTMLElement>(".activity-list > li")];
    expect(rows[0]?.textContent).toContain("整理分析目标");
    expect(rows[1]?.textContent).toContain("第 1 步 · 查询数据");
    mounted.unmount();
  });

  it("失败终态自动展开，并直接露出失败步骤", async () => {
    const failedItem = { ...activity().items[0]!, status: "failed" as const, detail: "未完成" };
    const failed = activity({
      status: "failed",
      completionKind: null,
      summary: "分析未完成",
      items: [failedItem],
      steps: [{
        ...activity().steps[0]!,
        status: "failed",
        detail: "1 个工具中有调用未完成",
        items: [failedItem],
      }],
      displayEntries: [{
        kind: "step",
        id: "step:run_1:1",
        seq: 2,
        stepNumber: 1,
        step: {
          ...activity().steps[0]!,
          status: "failed",
          detail: "1 个工具中有调用未完成",
          items: [failedItem],
        },
      }],
    });
    const mounted = await mountActivity(failed);

    expect(mounted.host.querySelector(".activity-list")).not.toBeNull();
    expect(mounted.host.querySelector(".activity-tool-list")).not.toBeNull();
    mounted.unmount();
  });

  it("部分完成使用警告状态并自动展开过程", async () => {
    const mounted = await mountActivity(activity({
      completionKind: "partial",
      summary: "分析部分完成",
    }));

    expect(mounted.host.querySelector(".activity-summary-icon.status-partial")).not.toBeNull();
    expect(mounted.host.querySelector(".activity-list")).not.toBeNull();
    mounted.unmount();
  });

  it("失败步骤把状态收成徽章，错误说明只出现一次", async () => {
    const failedItem = { ...activity().items[0]!, status: "failed" as const, detail: "未完成" };
    const mounted = await mountActivity(activity({
      status: "failed",
      completionKind: null,
      summary: "分析未完成",
      items: [failedItem],
      steps: [{
        ...activity().steps[0]!,
        status: "failed",
        detail: "Agent 回合未完成：MODEL_OUTPUT_INVALID",
        result: "回合未完成：MODEL_OUTPUT_INVALID",
        items: [failedItem],
      }],
      displayEntries: [{
        kind: "step",
        id: "step:run_1:1",
        seq: 2,
        stepNumber: 1,
        step: {
          ...activity().steps[0]!,
          status: "failed",
          detail: "Agent 回合未完成：MODEL_OUTPUT_INVALID",
          result: "回合未完成：MODEL_OUTPUT_INVALID",
          items: [failedItem],
        },
      }],
    }));

    const text = mounted.host.textContent ?? "";
    expect(mounted.host.querySelector(".activity-status.status-failed")).not.toBeNull();
    expect(text).toContain("未完成");
    expect(text).toContain("回合未完成：MODEL_OUTPUT_INVALID");
    expect(text.match(/MODEL_OUTPUT_INVALID/g)).toHaveLength(1);
    expect(text).not.toContain("Agent 回合未完成：MODEL_OUTPUT_INVALID");
    mounted.unmount();
  });
});
