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
  /** 收到 401/403 时回调（业务通道挂会话清理：回退匿名，防死循环 401）。 */
  onUnauthorized?: (status: number) => void;
  fetchImpl?: FetchLike;
}

export class CloudHttpClient {
  private readonly baseUrl: string;
  private readonly getToken: TokenProvider;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly retryBackoffMs: number;
  private readonly onUnauthorized?: (status: number) => void;
  private readonly fetchImpl: FetchLike;

  constructor(options: CloudHttpOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.getToken = options.getToken ?? (async () => null);
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.maxRetries = options.maxRetries ?? 2;
    this.retryBackoffMs = options.retryBackoffMs ?? 400;
    this.onUnauthorized = options.onUnauthorized;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
  }

  /** GET：信封解包返回 data。 */
  async get<T = unknown>(path: string): Promise<T> {
    return this.request<T>(path, { method: "GET" });
  }

  /**
   * GET 裸文本（非信封响应；如积分流水 CSV 导出）。
   * 401/403 走统一清会话回调；4xx/5xx 按 HTTP 错误抛出（裸文本不含业务错误码可解）。
   */
  async getRaw(path: string): Promise<string> {
    return this.request(
      path,
      { method: "GET", headers: { accept: "text/csv, text/plain;q=0.9, */*;q=0.8" } },
      (text, status) => {
        if (status >= 400) {
          throw new CloudApiError(null, status, `云端请求失败（HTTP ${status}）`);
        }
        return text;
      },
    );
  }

  /** POST：JSON 请求体，信封解包返回 data。 */
  async post<T = unknown>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  /**
   * 请求主流程（重试/鉴权/信封或裸文本解析）。
   * parse 缺省按统一信封解包（业务错误码归一）；裸文本通道注入自定义 parse。
   */
  private async request<T>(
    path: string,
    init: RequestInit,
    parse?: (text: string, status: number) => T,
  ): Promise<T> {
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
          try {
            this.onUnauthorized?.(response.status);
          } catch {
            // 回调异常不改变请求语义：仍按未授权抛出
          }
          throw new CloudApiError(null, response.status, `未授权（HTTP ${response.status}）`);
        }
        const text = await response.text();
        if (!response.ok && response.status >= 500) {
          // 5xx：可重试（服务端幂等语义兜底）
          throw new CloudApiError(null, response.status, `云端暂时不可用（HTTP ${response.status}）`);
        }
        return parse ? parse(text, response.status) : unwrapEnvelope<T>(parseJson(text, response.status), response.status);
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