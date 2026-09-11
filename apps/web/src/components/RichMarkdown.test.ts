import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";

import { applyTheme } from "@/lib/theme";

import RichMarkdown from "./RichMarkdown.vue";

async function mountMarkdown(
  content: string,
  answer = false,
  density: "chat" | "artifact" = "chat",
  anchorPrefix = "",
  streaming = false,
): Promise<{ host: HTMLElement; unmount: () => void }> {
  const app = createApp(RichMarkdown, { content, answer, density, anchorPrefix, streaming });
  const host = document.createElement("div");
  document.body.append(host);
  app.mount(host);
  await nextTick();
  return { host, unmount: () => app.unmount() };
}

describe("RichMarkdown", () => {
  afterEach(() => {
    applyTheme("dark");
    document.body.replaceChildren();
  });

  it("保留 Markdown 的层级结构和表格语义", async () => {
    const mounted = await mountMarkdown([
      "# 贷款用途统计",
      "",
      "## 结果",
      "",
      "| 用途 | 数量 |",
      "| --- | ---: |",
      "| small_business | 619 |",
    ].join("\n"));

    expect(mounted.host.querySelector("h1")?.textContent).toBe("贷款用途统计");
    expect(mounted.host.querySelector("h2")?.textContent).toBe("结果");
    expect(mounted.host.querySelector("table")).not.toBeNull();
    expect(mounted.host.querySelector("th")?.textContent).toBe("用途");
    mounted.unmount();
  });

  it("为答案标题生成可供 Overview 定位的锚点", async () => {
    const mounted = await mountMarkdown(
      "# 总结\n\n## 关键发现",
      true,
      "chat",
      "answer-message-1",
    );

    expect(mounted.host.querySelector("h1")?.id).toBe("answer-message-1-heading-1");
    expect(mounted.host.querySelector("h2")?.id).toBe("answer-message-1-heading-2");
    mounted.unmount();
  });

  it("正式答案隐藏内部证据编号但保留可读内容", async () => {
    const mounted = await mountMarkdown("查询已审计 audit_a82780b2baa1，结果见 artifact_a1b2c3d4。", true);

    expect(mounted.host.textContent).toContain("本次审计记录");
    expect(mounted.host.textContent).toContain("本次分析产物");
    expect(mounted.host.textContent).not.toMatch(/(?:audit|artifact)_/u);
    mounted.unmount();
  });

  it("Markdown 产物使用同一渲染器和产物密度", async () => {
    const mounted = await mountMarkdown("## 产物说明\n\n- 已登记", false, "artifact");

    expect(mounted.host.querySelector(".rich-markdown-artifact .markstream-vue")).not.toBeNull();
    expect(mounted.host.querySelector("h2")?.textContent).toBe("产物说明");
    mounted.unmount();
  });

  it("流式答案使用非最终渲染状态", async () => {
    const mounted = await mountMarkdown("# 逐步生成", true, "chat", "streaming", true);
    expect(mounted.host.querySelector("h1")?.textContent).toBe("逐步生成");
    mounted.unmount();
  });

  it("在深色工作台中启用 markstream 暗色主题", async () => {
    const mounted = await mountMarkdown("## 各渠道客户数");

    expect(mounted.host.querySelector(".markstream-vue.dark")).not.toBeNull();
    mounted.unmount();
  });

  it("浅色工作台关闭 markstream 暗色主题", async () => {
    applyTheme("light");
    const mounted = await mountMarkdown("## 各渠道客户数");

    expect(mounted.host.querySelector(".markstream-vue.dark")).toBeNull();
    mounted.unmount();
  });

  it("表格保持真实 table 布局以便列对齐", async () => {
    const mounted = await mountMarkdown([
      "| 获客渠道 | 客户数 |",
      "| --- | ---: |",
      "| organic | 679 |",
    ].join("\n"));

    const table = mounted.host.querySelector("table");
    expect(table).not.toBeNull();
    expect(getComputedStyle(table as HTMLTableElement).display).toBe("table");
    expect(getComputedStyle(table?.querySelector("thead") as HTMLElement).display).not.toBe("table");
    mounted.unmount();
  });

  it("把全数字列右对齐并保留文本列", async () => {
    const mounted = await mountMarkdown([
      "| 获客渠道 | 客户数 |",
      "| --- | ---: |",
      "| organic | 679 |",
      "| email | 245 |",
    ].join("\n"));
    await nextTick();

    const cells = [...mounted.host.querySelectorAll("td")];
    expect(cells[0]?.classList.contains("rich-md-numeric")).toBe(false);
    expect(cells[1]?.classList.contains("rich-md-numeric")).toBe(true);
    expect(mounted.host.querySelector("th:last-child")?.classList.contains("rich-md-numeric")).toBe(true);
    mounted.unmount();
  });

  it("将原始 HTML 和危险链接留在安全边界内", async () => {
    const mounted = await mountMarkdown(
      'safe <img src=x onerror=alert(1)> [bad](javascript:alert(1)) [good](https://example.com)',
    );
    const renderer = mounted.host.querySelector(".markstream-vue");

    expect(renderer?.querySelector("img")).toBeNull();
    expect(renderer?.textContent).toContain("<img");
    expect(renderer?.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(renderer?.querySelector('a[href="https://example.com"]')).not.toBeNull();
    mounted.unmount();
  });
});
