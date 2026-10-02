export interface ApiErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface ApiEnvelope<T> {
  data: T | null;
  request_id: string;
  error: ApiErrorBody | null;
}

export interface BinaryResponse {
  blob: Blob;
  contentType: string;
  filename: string | null;
}

export interface HealthResponse {
  status: string;
  metadata: string;
  datasource_root: string;
  artifact_root: string;
  datalink: string;
}

export class ApiClientError extends Error {
  readonly code: string;
  readonly requestId: string;
  readonly details: Record<string, unknown>;

  constructor(error: ApiErrorBody, requestId: string) {
    super(error.message);
    this.name = "ApiClientError";
    this.code = error.code;
    this.requestId = requestId;
    this.details = error.details;
  }
}

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

function resolveApiBaseUrl(): string {
  if (!import.meta.env.DEV || typeof window === "undefined") {
    return configuredApiBaseUrl;
  }
  const pageHost = window.location.hostname;
  if (pageHost === "localhost" || pageHost === "127.0.0.1" || pageHost === "") {
    return configuredApiBaseUrl;
  }
  try {
    const url = new URL(configuredApiBaseUrl);
    url.hostname = pageHost;
    return url.origin;
  } catch {
    return configuredApiBaseUrl;
  }
}

const apiBaseUrl = resolveApiBaseUrl();

function joinUrl(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

function healthUrl(): string {
  return joinUrl(apiBaseUrl, "/health");
}

/** 给 JSON、SSE 和二进制请求共用的受控地址拼接。 */
export function buildApiUrl(path: string): string {
  return joinUrl(apiBaseUrl, path);
}

/**
 * 合并调用方请求头；FormData 交给浏览器生成 multipart 边界，其他请求默认使用 JSON。
 */
function buildHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers);
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;

  // 浏览器会为 FormData 自动补充 multipart boundary，不能手动覆盖。
  if (!isFormData && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }

  return headers;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseApiEnvelope(value: unknown): ApiEnvelope<unknown> | null {
  if (!isRecord(value) || typeof value.request_id !== "string") {
    return null;
  }
  const error = value.error;
  if (error === null) {
    return { data: "data" in value ? value.data : null, request_id: value.request_id, error: null };
  }
  if (!isRecord(error) || typeof error.code !== "string" || typeof error.message !== "string") {
    return null;
  }
  const details = error.details;
  if (!isRecord(details) || Array.isArray(details)) {
    return null;
  }
  return {
    data: "data" in value ? value.data : null,
    request_id: value.request_id,
    error: { code: error.code, message: error.message, details },
  };
}

async function readJsonBody(response: Response): Promise<unknown | null> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function errorFromResponse(payload: unknown): ApiClientError {
  const envelope = parseApiEnvelope(payload);
  return new ApiClientError(
    envelope?.error ?? {
      code: "INTERNAL_ERROR",
      message: "Unexpected API response",
      details: {},
    },
    envelope?.request_id ?? "req_unknown",
  );
}

/** 给 SSE 等非 JSON 信道复用统一错误信封解析。 */
export async function throwApiError(response: Response): Promise<never> {
  throw errorFromResponse(await readJsonBody(response));
}

/** 调用统一 JSON 信封接口，并把服务端错误转换成 ApiClientError。 */
export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers: buildHeaders(init),
  });
  const payload = await readJsonBody(response);
  const envelope = parseApiEnvelope(payload);
  if (!response.ok || envelope?.error) {
    throw errorFromResponse(payload);
  }
  if (envelope === null) {
    throw errorFromResponse(payload);
  }
  return envelope.data as T;
}

function parseFilename(contentDisposition: string | null): string | null {
  if (contentDisposition === null) {
    return null;
  }
  const match = /filename="?([^";]+)"?/i.exec(contentDisposition);
  return match?.[1] ?? null;
}

/** 读取 Artifact 裸正文；失败时仍按统一 JSON 错误信封处理。 */
export async function requestBinary(path: string, init?: RequestInit): Promise<BinaryResponse> {
  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers: buildHeaders(init),
  });
  if (!response.ok) {
    return throwApiError(response);
  }
  return {
    blob: await response.blob(),
    contentType: response.headers.get("content-type") ?? "application/octet-stream",
    filename: parseFilename(response.headers.get("content-disposition")),
  };
}

/** 健康检查不使用业务 JSON 信封，用于启动探测和开发环境连通性确认。 */
export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(healthUrl());
  if (!response.ok) {
    throw new Error("Health check failed");
  }
  return (await response.json()) as HealthResponse;
}
