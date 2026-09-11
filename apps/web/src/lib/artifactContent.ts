import type { RunArtifact, Scalar, TableData } from "@/api/types";

export type ArtifactReaderKind =
  | "table"
  | "markdown"
  | "json"
  | "text"
  | "csv"
  | "tsv"
  | "image"
  | "download";

export interface ArtifactTableSort {
  columnIndex: number;
  direction: "asc" | "desc";
}

const tableCollator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isScalar(value: unknown): value is Scalar {
  return value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

/** 只接受后端表格 Artifact 的受限 JSON 形状，拒绝任意嵌套内容。 */
export function parseTableArtifact(text: string): TableData | null {
  let value: unknown;
  try {
    value = JSON.parse(text) as unknown;
  } catch {
    return null;
  }
  if (!isRecord(value) || !Array.isArray(value.columns) || !Array.isArray(value.rows)) return null;
  if (!value.columns.every((column) => typeof column === "string")) return null;
  if (typeof value.row_count !== "number" || !Number.isInteger(value.row_count) || value.row_count < 0) {
    return null;
  }
  const rows: Scalar[][] = [];
  for (const row of value.rows) {
    if (!Array.isArray(row) || row.length !== value.columns.length || !row.every(isScalar)) {
      return null;
    }
    rows.push(row);
  }
  if (value.row_count < rows.length) return null;
  return { columns: value.columns, rows, row_count: value.row_count };
}

/** 解析 UTF-8 CSV/TSV，支持引号、转义引号和引号内换行。 */
export function parseDelimitedArtifact(text: string, delimiter: "," | "\t"): TableData | null {
  const source = text.replace(/^\uFEFF/u, "");
  const records: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let afterClosingQuote = false;
  let endedWithRecordSeparator = false;

  const pushRecord = (): void => {
    row.push(field);
    records.push(row);
    row = [];
    field = "";
  };

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (quoted) {
      if (character === '"') {
        if (source[index + 1] === '"') {
          field += '"';
          index += 1;
        } else {
          quoted = false;
          afterClosingQuote = true;
        }
      } else {
        field += character;
      }
      endedWithRecordSeparator = false;
      continue;
    }

    if (afterClosingQuote) {
      if (character === delimiter) {
        row.push(field);
        field = "";
        afterClosingQuote = false;
        endedWithRecordSeparator = false;
      } else if (character === "\n" || character === "\r") {
        if (character === "\r" && source[index + 1] === "\n") index += 1;
        pushRecord();
        afterClosingQuote = false;
        endedWithRecordSeparator = true;
      } else {
        return null;
      }
    } else if (character === '"') {
      if (field.length > 0) return null;
      quoted = true;
      endedWithRecordSeparator = false;
    } else if (character === delimiter) {
      row.push(field);
      field = "";
      endedWithRecordSeparator = false;
    } else if (character === "\n" || character === "\r") {
      if (character === "\r" && source[index + 1] === "\n") index += 1;
      pushRecord();
      endedWithRecordSeparator = true;
    } else {
      field += character;
      endedWithRecordSeparator = false;
    }
  }

  if (quoted) return null;
  if (!endedWithRecordSeparator || row.length > 0 || field.length > 0) pushRecord();
  if (records.length === 0 || records[0]?.every((cell) => cell.length === 0)) return null;

  const header = records[0] ?? [];
  const dataRows = records.slice(1);
  const columnCount = Math.max(header.length, ...dataRows.map((record) => record.length));
  const columns = Array.from({ length: columnCount }, (_, index) => {
    const name = header[index]?.trim();
    return name ? name : `列 ${index + 1}`;
  });
  const rows = dataRows.map((record) =>
    Array.from({ length: columnCount }, (_, index) => record[index] ?? ""),
  );
  return { columns, rows, row_count: rows.length };
}

export function formatJsonArtifact(text: string): string | null {
  try {
    return JSON.stringify(JSON.parse(text.replace(/^\uFEFF/u, "")) as unknown, null, 2) ?? null;
  } catch {
    return null;
  }
}

export function normalizeArtifactMime(contentType: string): string {
  return contentType.split(";", 1)[0]?.trim().toLowerCase() ?? "";
}

/** 阅读器只使用后端登记的类型、MIME 和内联能力，不根据标题或路径后缀猜测。 */
export function artifactReaderKind(artifact: RunArtifact): ArtifactReaderKind {
  if (!artifact.inline_previewable) return "download";
  const mime = normalizeArtifactMime(artifact.mime_type);
  if (artifact.type === "table" && mime === "application/json") return "table";
  if (artifact.type === "markdown" && mime === "text/markdown") return "markdown";
  if (artifact.type === "chart" && isSafeChartMime(mime)) return "image";
  if (artifact.type !== "file") return "download";
  if (mime === "application/json") return "json";
  if (mime === "text/plain") return "text";
  if (mime === "text/csv") return "csv";
  if (mime === "text/tab-separated-values") return "tsv";
  return "download";
}

export function filterArtifactRows(rows: readonly Scalar[][], query: string): Scalar[][] {
  const normalized = query.trim().toLocaleLowerCase("zh-CN");
  if (!normalized) return [...rows];
  return rows.filter((row) =>
    row.some((cell) => formatArtifactCell(cell).toLocaleLowerCase("zh-CN").includes(normalized)),
  );
}

export function sortArtifactRows(
  rows: readonly Scalar[][],
  sort: ArtifactTableSort | null,
): Scalar[][] {
  if (sort === null) return [...rows];
  return rows
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const compared = compareArtifactCells(
        left.row[sort.columnIndex] ?? null,
        right.row[sort.columnIndex] ?? null,
      );
      return (sort.direction === "asc" ? compared : -compared) || left.index - right.index;
    })
    .map(({ row }) => row);
}

function compareArtifactCells(left: Scalar, right: Scalar): number {
  if (left === null) return right === null ? 0 : 1;
  if (right === null) return -1;
  const leftNumber = numericValue(left);
  const rightNumber = numericValue(right);
  if (leftNumber !== null && rightNumber !== null) return leftNumber - rightNumber;
  return tableCollator.compare(String(left), String(right));
}

function numericValue(value: Exclude<Scalar, null>): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Chart 只作为 img 展示；SVG 已在后端登记前通过内容安全校验。 */
export function isSafeChartMime(contentType: string): boolean {
  const mime = normalizeArtifactMime(contentType);
  return mime === "image/png" || mime === "image/svg+xml";
}

export function formatArtifactCell(value: unknown): string {
  if (value === null) return "-";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "(无效值)";
}
