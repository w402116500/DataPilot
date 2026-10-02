import { describe, expect, it } from "vitest";

import type { RunArtifact } from "@/api/types";

import {
  artifactReaderKind,
  filterArtifactRows,
  formatJsonArtifact,
  isSafeChartMime,
  parseDelimitedArtifact,
  parseTableArtifact,
  sortArtifactRows,
} from "./artifactContent";

function artifact(overrides: Partial<RunArtifact> = {}): RunArtifact {
  return {
    id: "artifact_1",
    run_id: "run_1",
    session_id: "session_1",
    tool_call_id: "tool_1",
    datasource_deleted: false,
    type: "file",
    title: "not-a-format-hint.xlsx",
    mime_type: "text/csv",
    size_bytes: 20,
    inline_previewable: true,
    preview: null,
    metadata: null,
    content_hash: "hash_1",
    created_at: "2026-08-16T00:00:00Z",
    ...overrides,
  };
}

describe("artifactContent", () => {
  it("only accepts the constrained table artifact shape", () => {
    expect(parseTableArtifact('{"columns":["amount"],"rows":[[100]],"row_count":1}')).toEqual({
      columns: ["amount"],
      rows: [[100]],
      row_count: 1,
    });
    expect(parseTableArtifact('{"columns":["amount"],"rows":[[{"raw":"value"}]],"row_count":1}')).toBeNull();
    expect(parseTableArtifact('{"columns":["amount"],"rows":[[1,2]],"row_count":1}')).toBeNull();
    expect(parseTableArtifact("not-json")).toBeNull();
  });

  it("parses quoted CSV and TSV fields including embedded newlines", () => {
    expect(parseDelimitedArtifact('name,note\r\n"Ada, A.","line 1\nline 2"\r\n"Lin","said ""hi"""\r\n', ","))
      .toEqual({
        columns: ["name", "note"],
        rows: [["Ada, A.", "line 1\nline 2"], ["Lin", 'said "hi"']],
        row_count: 2,
      });
    expect(parseDelimitedArtifact("name\tamount\nAda\t10\n", "\t")?.rows).toEqual([["Ada", "10"]]);
    expect(parseDelimitedArtifact('name,note\nAda,"unfinished', ",")).toBeNull();
    expect(parseDelimitedArtifact('name,note\nAda,"closed"suffix', ",")).toBeNull();
  });

  it("formats JSON and reports invalid JSON", () => {
    expect(formatJsonArtifact('{"ok":true,"items":[1,2]}')).toBe(
      '{\n  "ok": true,\n  "items": [\n    1,\n    2\n  ]\n}',
    );
    expect(formatJsonArtifact("{broken")).toBeNull();
  });

  it("filters and stably sorts loaded table rows", () => {
    const rows = [["beta", 10], ["Alpha", 2], ["alpha", 2]];
    expect(filterArtifactRows(rows, "ALP")).toEqual([["Alpha", 2], ["alpha", 2]]);
    expect(sortArtifactRows(rows, { columnIndex: 1, direction: "asc" })).toEqual([
      ["Alpha", 2],
      ["alpha", 2],
      ["beta", 10],
    ]);
  });

  it("selects file readers from registered MIME instead of the title suffix", () => {
    expect(artifactReaderKind(artifact())).toBe("csv");
    expect(artifactReaderKind(artifact({ mime_type: "application/json" }))).toBe("json");
    expect(artifactReaderKind(artifact({ mime_type: "text/plain" }))).toBe("text");
    expect(artifactReaderKind(artifact({ mime_type: "application/octet-stream" }))).toBe("download");
    expect(artifactReaderKind(artifact({ inline_previewable: false }))).toBe("download");
  });

  it("allows PNG and backend-validated SVG chart MIME types", () => {
    expect(isSafeChartMime("image/png")).toBe(true);
    expect(isSafeChartMime("image/svg+xml; charset=utf-8")).toBe(true);
    expect(isSafeChartMime("image/webp")).toBe(false);
    expect(isSafeChartMime("text/html")).toBe(false);
  });
});
