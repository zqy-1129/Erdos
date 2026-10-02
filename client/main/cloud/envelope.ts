/**
 * 云端信封与错误（SP3-5 云端集成客户端）。
 *
 * 与服务端统一响应信封对齐：{ code, message, detail, data, request_id, timestamp }；
 * code=0 成功，非 0 即业务错误（与 contracts/openapi.yaml x-error-codes 一一对应）。
 */

/** 服务端统一信封。 */
export interface CloudEnvelope {
  code: number;
  message: string;
  detail: string | null;
  data?: unknown;
  request_id?: string;
  timestamp?: string;
}

/** 云端错误：HTTP 层错误 code 为 null；业务错误携带服务端错误码。 */
export class CloudApiError extends Error {
  /** 服务端业务码（HTTP 层失败为 null）。 */
  readonly code: number | null;
  readonly httpStatus: number;

  constructor(code: number | null, httpStatus: number, message: string) {
    super(message);
    this.name = "CloudApiError";
    this.code = code;
    this.httpStatus = httpStatus;
  }

  get isBusiness(): boolean {
    return this.code !== null;
  }
}

function isEnvelope(value: unknown): value is CloudEnvelope {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as Record<string, unknown>)["code"] === "number"
  );
}

/** 解析响应体为信封；code≠0 抛 CloudApiError；返回 data。 */
export function unwrapEnvelope<T = unknown>(body: unknown, httpStatus: number): T {
  if (!isEnvelope(body)) {
    throw new CloudApiError(null, httpStatus, "响应不是统一信封格式");
  }
  if (body.code !== 0) {
    const detail = body.detail ? `：${body.detail}` : "";
    throw new CloudApiError(body.code, httpStatus, `${body.message}${detail}`);
  }
  return body.data as T;
}

/** 解析 JSON 响应文本；非法 JSON 抛协议错误。 */
export function parseJson(text: string, httpStatus: number): unknown {
  try {
    return JSON.parse(text);
  } catch {
    throw new CloudApiError(null, httpStatus, "响应不是合法 JSON");
  }
}