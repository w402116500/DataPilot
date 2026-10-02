import { createApp, defineComponent, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AnswerEvidenceSnapshot, RunArtifact, SqlAudit } from "@/api/types";

vi.mock("./ArtifactViewer.vue", () => ({
  default: defineComponent({
    props: ["artifact"],
    template: '<div data-testid="artifact-detail">{{ artifact.title }}</div>',
  }),
}));

import AnswerMaterialViewer from "./AnswerMaterialViewer.vue";

const snapshot: AnswerEvidenceSnapshot = {
  version: 1,
  materials: [{
    number: 1,
    kind: "chart",
    title: "销量趋势",
    content: "展示销量变化。",
    scope: "2025 年已获得的数据",
    sources: [{ kind: "artifact", ref_id: "artifact-1", label: "销量图", schema_revision: null, graph_version: null }],
  }],
  cited_numbers: [1],
  invalid_numbers: [],
};

describe("AnswerMaterialViewer", () => {
  afterEach(() => document.body.replaceChildren());

  it("先展示保存的材料，用户要求时才展开产物详情", async () => {
    const artifact = { id: "artifact-1", title: "销量图" } as RunArtifact;
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp(AnswerMaterialViewer, { snapshot, selectedNumber: 1, artifacts: [artifact], audits: [] });
    app.mount(host);
    await nextTick();

    expect(document.body.textContent).toContain("展示销量变化。");
    expect(document.body.querySelector('[data-testid="artifact-detail"]')).toBeNull();
    const button = [...document.body.querySelectorAll("button")].find(item => item.textContent?.includes("查看销量图详情"));
    expect(button).toBeDefined();
    button?.click();
    await nextTick();
    expect(document.body.querySelector('[data-testid="artifact-detail"]')?.textContent).toBe("销量图");
    app.unmount();
  });

  it("产物记录缺失时仍保留材料文字", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp(AnswerMaterialViewer, { snapshot, selectedNumber: 1, artifacts: [], audits: [] });
    app.mount(host);
    await nextTick();
    expect(document.body.textContent).toContain("展示销量变化。");
    expect(document.body.textContent).toContain("详情尚未加载或已不可用");
    app.unmount();
  });

  it("只展示当前材料关联的查询审计摘要", async () => {
    const withAudit: AnswerEvidenceSnapshot = {
      ...snapshot,
      materials: [{
        ...snapshot.materials[0]!,
        sources: [{ kind: "audit", ref_id: "audit-1", label: "查询结果", schema_revision: 1, graph_version: null }],
      }],
    };
    const audits = [
      { id: "audit-1", attempt_no: 2, status: "succeeded", referenced_tables: ["orders"], row_count: 12 },
      { id: "audit-2", attempt_no: 3, status: "failed", referenced_tables: ["private"], row_count: null },
    ] as SqlAudit[];
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp(AnswerMaterialViewer, { snapshot: withAudit, selectedNumber: 1, artifacts: [], audits });
    app.mount(host);
    await nextTick();
    const summary = document.body.querySelector('[aria-label="SQL 审计摘要"]');
    expect(summary?.textContent).toContain("第 3 次");
    expect(summary?.textContent).toContain("orders");
    expect(summary?.textContent).not.toContain("private");
    app.unmount();
  });
});
