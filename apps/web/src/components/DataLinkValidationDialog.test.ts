import { createApp, h, nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";
import type { DataLinkValidation } from "@/api/types";
import DataLinkValidationDialog from "./DataLinkValidationDialog.vue";

const validation: DataLinkValidation = {
  id: "datalink_validation_1",
  datasource_id: "ds_1",
  relation_id: "rel_1",
  graph_version: "graph_1",
  schema_revision: 2,
  status: "completed",
  source_non_null_count: 3,
  target_non_null_count: 4,
  source_distinct_count: 3,
  target_distinct_count: 2,
  target_duplicate_count: 1,
  source_unmatched_count: 1,
  multiple_match_risk: true,
  direction: "source_to_target",
  endpoint_fingerprint: "abc",
  audit_log_ids: ["audit_1"],
  artifact_ids: ["artifact_1"],
  error_code: null,
  expired: false,
  created_at: "2026-09-06T00:00:00Z",
  finished_at: "2026-09-06T00:01:00Z",
};

let cleanup: (() => void) | undefined;
afterEach(() => { cleanup?.(); cleanup = undefined; document.body.replaceChildren(); });

describe("DataLinkValidationDialog", () => {
  it("shows validation metrics and does not treat data checks as business proof", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp({ render: () => h(DataLinkValidationDialog, { validation }) });
    app.mount(host);
    cleanup = () => { app.unmount(); host.remove(); };
    await nextTick();
    expect(document.body.textContent).toContain("源未匹配");
    expect(document.body.textContent).toContain("多重匹配风险");
    expect(document.body.textContent).toContain("存在");
    expect(document.body.textContent).toContain("数据检查通过不等于业务关系正确");
  });
});
