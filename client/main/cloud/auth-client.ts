/**
 * 鉴权客户端与登录态（SP3-5）：对接服务端 /v1/auth/login|refresh|logout。
 *
 * - AuthClient：登录/刷新/注销（信封解包 + 载荷形状校验），匿名通道；
 * - SessionTokenProvider：内存令牌 + 过期自动轮换（skew 提前量）+
 *   并发轮换去重，经 getToken（适配为 TokenProvider 函数）接入 CloudHttpClient。
 * 令牌落盘（本地安全存储）在 SP3-4 接入；本模块默认内存存储。
 */

import { CloudApiError } from "./envelope.ts";
import { CloudHttpClient } from "./http.ts";

/** 服务端双令牌视图（TokenPairView）。 */
export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  /** 访问令牌 TTL（秒）。 */
  expires_in: number;
  /** 刷新令牌 TTL（秒）。 */
  refresh_expires_in: number;
}

/** 持久化会话：令牌集 + 签发时刻（恢复后计算剩余有效期）。 */
export interface StoredSession {
  pair: TokenPair;
  /** 签发时刻（ms 时间戳）。 */
  issued_at_ms: number;
}

/** 令牌存储（内存实现为默认；SP3-4 接本地安全存储）。 */
export interface TokenStore {
  save(session: StoredSession | null): void;
  load(): StoredSession | null;
}

export class InMemoryTokenStore implements TokenStore {
  private current: StoredSession | null = null;
  save(session: StoredSession | null): void {
    this.current = session;
  }
  load(): StoredSession | null {
    return this.current;
  }
}

function parseTokenPair(value: unknown): TokenPair {
  if (typeof value !== "object" || value === null) {
    throw new Error("令牌响应形状非法");
  }
  const v = value as Record<string, unknown>;
  if (
    typeof v["access_token"] !== "string" ||
    typeof v["refresh_token"] !== "string" ||
    typeof v["token_type"] !== "string" ||
    typeof v["expires_in"] !== "number" ||
    typeof v["refresh_expires_in"] !== "number"
  ) {
    throw new Error("令牌响应形状非法");
  }
  return {
    access_token: v["access_token"],
    refresh_token: v["refresh_token"],
    token_type: v["token_type"],
    expires_in: v["expires_in"],
    refresh_expires_in: v["refresh_expires_in"],
  };
}

/** 鉴权客户端（匿名通道；业务请求经 SessionTokenProvider 注入令牌的独立通道）。 */
export class AuthClient {
  private readonly http: CloudHttpClient;

  constructor(http: CloudHttpClient) {
    this.http = http;
  }

  /** 密码登录（device_id 可选，用于多设备会话）。 */
  async login(username: string, password: string, deviceId?: string): Promise<TokenPair> {
    const body: Record<string, unknown> = { username, password };
    if (deviceId) body["device_id"] = deviceId;
    return parseTokenPair(await this.http.post("/v1/auth/login", body));
  }

  /** 刷新轮换（服务端换代防护：旧刷新令牌立即失效）。 */
  async refresh(refreshToken: string, deviceId?: string): Promise<TokenPair> {
    const body: Record<string, unknown> = { refresh_token: refreshToken };
    if (deviceId) body["device_id"] = deviceId;
    return parseTokenPair(await this.http.post("/v1/auth/refresh", body));
  }

  /** 注销（设备维度吊销，幂等）。 */
  async logout(refreshToken: string): Promise<number> {
    const value = await this.http.post<Record<string, unknown>>("/v1/auth/logout", {
      refresh_token: refreshToken,
    });
    return Number(value["revoked"] ?? 0);
  }
}

export interface SessionOptions {
  auth: AuthClient;
  /** 令牌存储（默认内存；SP3-4 接本地安全存储）。 */
  store?: TokenStore;
  /** ms 时间戳时钟（测试注入）。 */
  clock?: () => number;
  /** 过期提前量：剩余有效期低于该值时提前轮换。 */
  skewMs?: number;
  /** 会话设备标识（轮换时携带，服务端校验设备一致性）。 */
  deviceId?: string;
}

const DEFAULT_SKEW_MS = 30_000;

/** 登录态提供者：内存令牌 + 过期自动轮换 + 并发轮换去重。 */
export class SessionTokenProvider {
  private readonly auth: AuthClient;
  private readonly store: TokenStore;
  private readonly clock: () => number;
  private readonly skewMs: number;
  private readonly deviceId?: string;
  private session: StoredSession | null = null;
  /** 并发轮换去重：同时到达的 getToken 共享同一次刷新。 */
  private rotation: Promise<string | null> | null = null;

  constructor(options: SessionOptions) {
    this.auth = options.auth;
    this.store = options.store ?? new InMemoryTokenStore();
    this.clock = options.clock ?? (() => Date.now());
    this.skewMs = options.skewMs ?? DEFAULT_SKEW_MS;
    this.deviceId = options.deviceId;
    this.session = this.store.load();
  }

  /** 登录并登记会话（返回令牌集；供显式登录流程调用）。 */
  async login(username: string, password: string, deviceId?: string): Promise<TokenPair> {
    const pair = await this.auth.login(username, password, deviceId ?? this.deviceId);
    this.apply(pair);
    return pair;
  }

  /** 是否已登录（当前内存会话存在）。 */
  signedIn(): boolean {
    return this.session !== null;
  }

  /** 注销并清空会话（吊销失败原样抛出，本地会话已清）。 */
  async logout(): Promise<number> {
    const session = this.session;
    this.clear();
    if (!session) return 0;
    return this.auth.logout(session.pair.refresh_token);
  }

  /** TokenProvider 适配：CloudHttpClient.getToken 可直接引用。 */
  getToken(): Promise<string | null> {
    const session = this.session;
    if (session && !this.expiring(session)) {
      return Promise.resolve(session.pair.access_token);
    }
    return this.rotate();
  }

  private apply(pair: TokenPair): StoredSession {
    const session: StoredSession = { pair, issued_at_ms: this.clock() };
    this.session = session;
    this.store.save(session);
    return session;
  }

  private clear(): void {
    this.session = null;
    this.store.save(null);
  }

  private expiring(session: StoredSession): boolean {
    return session.pair.expires_in <= 0 ||
      this.clock() + this.skewMs >= session.issued_at_ms + session.pair.expires_in * 1000;
  }

  private rotate(): Promise<string | null> {
    if (!this.rotation) {
      this.rotation = this.doRotate().finally(() => {
        this.rotation = null;
      });
    }
    return this.rotation;
  }

  private async doRotate(): Promise<string | null> {
    const session = this.session;
    if (!session) return null;
    if (!this.expiring(session)) {
      return session.pair.access_token; // 等锁期间已被其它请求刷新
    }
    try {
      const pair = await this.auth.refresh(session.pair.refresh_token, this.deviceId);
      this.apply(pair);
      return pair.access_token;
    } catch (error) {
      if (error instanceof CloudApiError && (error.httpStatus === 401 || error.httpStatus === 403)) {
        this.clear(); // 刷新令牌失效 → 会话终止，回退匿名
        return null;
      }
      throw error; // 网络/5xx：保留会话，交由上游重试
    }
  }
}

export interface SessionAuthOptions {
  baseUrl: string;
  store?: TokenStore;
  clock?: () => number;
  skewMs?: number;
  deviceId?: string;
  timeoutMs?: number;
  maxRetries?: number;
}

/** 装配鉴权通道：匿名 AuthClient + 登录态提供者；getToken 为已绑定的 TokenProvider。 */
export function createSessionAuth(options: SessionAuthOptions): {
  client: AuthClient;
  tokens: SessionTokenProvider;
  getToken: () => Promise<string | null>;
} {
  const anonymous = new CloudHttpClient({
    baseUrl: options.baseUrl,
    timeoutMs: options.timeoutMs,
    maxRetries: options.maxRetries,
  });
  const client = new AuthClient(anonymous);
  const tokens = new SessionTokenProvider({
    auth: client,
    store: options.store,
    clock: options.clock,
    skewMs: options.skewMs,
    deviceId: options.deviceId,
  });
  return { client, tokens, getToken: () => tokens.getToken() };
}