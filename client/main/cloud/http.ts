/**
 * 云端 HTTP 客户端（SP3-5）：超时、Bearer 鉴权、网络重试（退避）、信封解包。
 *
 * 重试策略（可用性优先，资金域语义由服务端幂等保证）：
 * - 网络错误 / 5xx：最多 maxRetries 次（指数退避）；
 * - 401/403/其它 4xx：不重试，直接抛 CloudApiError。
 * fetchImpl 可注入（测试与后续替换传输层）。
 */

import { CloudApiError, parseJson, unwrapEnvelope } from "./envelope.ts";

export type FetchLike = (
  input: string,
  init: RequestInit,
) => Promise<Pick<Response, "status" | "ok" | "text">>;

/** 令牌提供者（SP3-5 接入登录态；null 表示匿名请求）。 */
export type TokenProvider = () => Promise<string | null>;

export interface CloudHttpOptions {
  baseUrl: string;
  getToken?: TokenProvider;
  timeoutMs?: number;
  maxRetries?: number;
  retryBackoffMs?: number;
  fetchImpl?: FetchLike;
}

export class CloudHttpClient {
  private readonly baseUrl: string;
  private readonly getToken: TokenProvider;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly retryBackoffMs: number;
  private readonly fetchImpl: FetchLike;

  constructor(options: CloudHttpOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.getToken = options.getToken ?? (async () => null);
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.maxRetries = options.maxRetries ?? 2;
    this.retryBackoffMs = options.retryBackoffMs ?? 400;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
  }

  /** GET：信封解包返回 data。 */
  async get<T = unknown>(path: string): Promise<T> {
    return this.request<T>(path, { method: "GET" });
  }

  /** POST：JSON 请求体，信封解包返回 data。 */
  async post<T = unknown>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  private async request<T>(path: string, init: RequestInit): Promise<T> {
    const attempts = this.maxRetries + 1;
    let lastError: unknown = null;
    for (let attempt = 0; attempt < attempts; attempt++) {
      if (attempt > 0) {
        await new Promise((r) => setTimeout(r, this.retryBackoffMs * 2 ** (attempt - 1)));
      }
      try {
        const headers: Record<string, string> = {
          accept: "application/json",
          ...((init.headers as Record<string, string> | undefined) ?? {}),
        };
        const token = await this.getToken();
        if (token) headers["authorization"] = `Bearer ${token}`;
        const response = await this.fetchImpl(this.baseUrl + path, {
          ...init,
          headers,
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (response.status === 401 || response.status === 403) {
          throw new CloudApiError(null, response.status, `未授权（HTTP ${response.status}）`);
        }
        const text = await response.text();
        if (!response.ok && response.status >= 500) {
          // 5xx：可重试（服务端幂等语义兜底）
          throw new CloudApiError(null, response.status, `云端暂时不可用（HTTP ${response.status}）`);
        }
        const body = parseJson(text, response.status);
        return unwrapEnvelope<T>(body, response.status);
      } catch (error) {
        lastError = error;
        if (error instanceof CloudApiError && error.httpStatus < 500) {
          throw error; // 4xx 业务/鉴权错误不重试
        }
        if (error instanceof CloudApiError) {
          continue; // 5xx 重试
        }
        // 网络错误/超时（AbortSignal）→ 重试
        continue;
      }
    }
    throw lastError instanceof CloudApiError
      ? lastError
      : new CloudApiError(
          0,
          0,
          `云端请求失败：${lastError instanceof Error ? lastError.message : String(lastError)}`,
        );
  }
}