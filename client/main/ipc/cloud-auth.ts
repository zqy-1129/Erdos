/**
 * 云端鉴权接线（SP3-5 云端集成 / SP2-2 服务端契约）：bridge 登录/注册/注销的真实对接层。
 *
 * 分层与职责：
 * - resolveAuthRuntime：ERDOS_API_BASE_URL + dev 标志 → 运行模式。开发期未配置 → demo
 *   （登录页演示回退，保持联调能力）；生产未配置 → unconfigured（fail-closed：
 *   登录直接报错，绝不冒充登录成功）；
 * - CloudAuthBridge：桥载荷 → 服务端契约请求（LoginRequest / RegisterCreate），
 *   令牌登记进 SessionTokenProvider 会话，返回渲染层 SessionView（渲染层不感知令牌）；
 * - classifyAuthError：服务端错误码 / HTTP 状态 → AuthFailureError（reason 归因 + 可读中文），
 *   与 key-probe 的 KeyTestOutcome 同一「分类」风格，供 UI 展示与日志定位。
 *
 * 本模块不依赖 electron（node:test 可直接加载）；令牌默认内存存储，
 * 落盘（本地安全存储）随后续批次接入（auth-client.ts 同注释）。
 */
import {
  createSessionAuth,
  type RegisterRequest,
  type TokenPair,
  type TokenStore,
  type AuthClient,
  type SessionTokenProvider,
} from "../cloud/auth-client.ts";
import { CloudApiError } from "../cloud/envelope.ts";
import type { FetchLike } from "../cloud/http.ts";

// ---------------------------------------------------------------------------
// 运行模式解析
// ---------------------------------------------------------------------------

/** 鉴权运行模式（index.ts 依环境解析；本模块只做纯判定）。 */
export type AuthRuntime =
  | { mode: "cloud"; baseUrl: string }
  | { mode: "demo" }
  | { mode: "unconfigured" };

/**
 * 解析鉴权运行模式：
 * - 配置了 ERDOS_API_BASE_URL → cloud（尾斜杠归一，交给 CloudHttpClient 拼接路径）；
 * - 未配置且开发期 → demo（演示回退，登录页 demo-* 账号仍可用）；
 * - 未配置且非开发期 → unconfigured（生产 fail-closed，不冒充登录成功）。
 */
export function resolveAuthRuntime(options: { apiBaseUrl?: string | null; dev: boolean }): AuthRuntime {
  const baseUrl = (options.apiBaseUrl ?? "").trim().replace(/\/+$/, "");
  if (baseUrl) return { mode: "cloud", baseUrl };
  return options.dev ? { mode: "demo" } : { mode: "unconfigured" };
}

// ---------------------------------------------------------------------------
// 会话视图与错误分类
// ---------------------------------------------------------------------------

/** 桥会话视图（渲染层 SessionView 同形：主进程归一，令牌不出主进程）。 */
export interface SessionView {
  username: string;
  expiresInMs: number;
}

/** 令牌对 → 会话视图（expires_in 秒 → ms；username 为登录标识原文）。 */
export function sessionViewOf(pair: TokenPair, username: string): SessionView {
  return { username, expiresInMs: pair.expires_in * 1000 };
}

/** 鉴权失败归因（UI 展示 message；reason 供日志/测试断言，风格对齐 KeyTestOutcome）。 */
export type AuthErrorReason =
  | "network" // 网络不可达 / 超时
  | "credentials" // 账号或密码错误（登录 401）
  | "locked" // 防爆破临时锁定（42301）
  | "frozen" // 账号冻结或已注销（登录 403）
  | "conflict" // 注册冲突：邮箱或手机号已注册（40901）
  | "invalid" // 参数不合法（本地校验 / 40001）
  | "unavailable" // 服务端 5xx
  | "unknown";

/** 鉴权失败错误：reason 归因 + 可读中文（bridge 直接抛给渲染层展示）。 */
export class AuthFailureError extends Error {
  readonly reason: AuthErrorReason;

  constructor(reason: AuthErrorReason, message: string) {
    super(message);
    this.name = "AuthFailureError";
    this.reason = reason;
  }
}

/**
 * 云端正版鉴权错误 → 可读归因。
 *
 * 归类顺序与可达性（对齐 http.ts 传输语义）：
 * - 401/403/5xx 由 CloudHttpClient 在解析信封前提前抛出（code=null），只能按 HTTP
 *   状态归类：401→凭证错误、403→账号冻结（登录路径语义）、5xx→服务不可用；
 * - 业务码仅对「透传信封」的响应可达：40001 参数错误、40901 注册冲突、42301 防爆破锁定；
 * - 其余非 0 业务码（如 42901 限流）透传服务端 message（本身即可读），归因 unknown。
 */
export function classifyAuthError(error: unknown): AuthFailureError {
  if (error instanceof AuthFailureError) return error;
  if (error instanceof CloudApiError) {
    const code = error.code;
    if (code === 40001) return new AuthFailureError("invalid", error.message);
    if (code === 40901) return new AuthFailureError("conflict", "该邮箱或手机号已注册，请直接登录");
    if (code === 42301) return new AuthFailureError("locked", "登录尝试过多，账号已临时锁定，请稍后再试");
    if (error.httpStatus === 0) {
      return new AuthFailureError("network", "无法连接云端服务：请检查网络连接与服务地址配置");
    }
    if (error.httpStatus === 401) return new AuthFailureError("credentials", "账号或密码错误，请检查后重试");
    if (error.httpStatus === 403) return new AuthFailureError("frozen", "账号已冻结或已注销");
    if (error.httpStatus >= 500) {
      return new AuthFailureError("unavailable", `云端服务异常（HTTP ${error.httpStatus}），请稍后重试`);
    }
    return new AuthFailureError("unknown", error.message);
  }
  const message = error instanceof Error ? error.message : String(error);
  return new AuthFailureError("unknown", message);
}

// ---------------------------------------------------------------------------
// 注册载荷映射（桥载荷 → 契约 RegisterCreate）
// ---------------------------------------------------------------------------

/** 桥注册载荷（登录页「注册并登录」仅采集账号 + 密码）。 */
export interface RegisterInput {
  username: string;
  password: string;
  /** 设备指纹（device-identity.ts 持久化生成）。 */
  fingerprint: string;
  platform?: string;
}

export type RegisterBodyResult = { ok: true; body: RegisterRequest } | { ok: false; message: string };

/**
 * 桥注册载荷 → 服务端 RegisterCreate：
 * - 账号含 @ → email（≤255），否则 → phone（≤32），与服务端 find_by_identifier 同口径；
 * - 密码、指纹、账号长度按契约前置校验（失败即本地报错，不发起请求）；
 * - 服务端契约 additionalProperties=false：不上送 device_id 等契约外字段。
 */
export function buildRegisterBody(input: RegisterInput): RegisterBodyResult {
  const username = input.username.trim();
  if (!username) return { ok: false, message: "请输入邮箱或手机号" };
  if (input.password.length < 8 || input.password.length > 128) {
    return { ok: false, message: "密码长度须为 8~128 位" };
  }
  const fingerprint = input.fingerprint.trim();
  if (fingerprint.length < 8 || fingerprint.length > 64) {
    return { ok: false, message: "设备指纹不可用：请重启客户端后重试" };
  }
  const body: RegisterRequest = { password: input.password, fingerprint };
  if (username.includes("@")) {
    if (username.length > 255) return { ok: false, message: "邮箱长度超出限制（最长 255 字符）" };
    body.email = username;
  } else {
    if (username.length > 32) return { ok: false, message: "手机号长度超出限制（最长 32 字符）" };
    body.phone = username;
  }
  if (input.platform) body.platform = input.platform;
  return { ok: true, body };
}

// ---------------------------------------------------------------------------
// CloudAuthBridge：bridge 通道 → 云端正版鉴权
// ---------------------------------------------------------------------------

export interface CloudAuthOptions {
  /** 云端服务基址（不含 /v1 前缀）。 */
  baseUrl: string;
  /** 设备指纹（注册赠分防刷；device-identity.ts 持久化生成）。 */
  fingerprint: string;
  /** 平台标识（win32/darwin/linux；登录设备登记与注册上送）。 */
  platform?: string;
  /** 传输注入（测试/联调）。 */
  fetchImpl?: FetchLike;
  timeoutMs?: number;
  maxRetries?: number;
  /** 令牌存储（默认内存；落盘随后续批次接入）。 */
  store?: TokenStore;
}

/** 云端正版鉴权桥：登录/注册/注销 + 会话查询（BridgeBackend 云模式使用）。 */
export class CloudAuthBridge {
  private readonly auth: AuthClient;
  private readonly tokens: SessionTokenProvider;
  private readonly fingerprint: string;
  private readonly platform?: string;

  constructor(options: CloudAuthOptions) {
    const session = createSessionAuth({
      baseUrl: options.baseUrl,
      store: options.store,
      timeoutMs: options.timeoutMs,
      maxRetries: options.maxRetries,
      fetchImpl: options.fetchImpl,
    });
    this.auth = session.client;
    this.tokens = session.tokens;
    this.fingerprint = options.fingerprint;
    this.platform = options.platform;
  }

  /** 密码登录：成功后登记会话，返回桥会话视图。 */
  async login(input: { username: string; password: string }): Promise<SessionView> {
    const username = input.username.trim();
    if (!username) throw new AuthFailureError("invalid", "请输入邮箱或手机号");
    try {
      const pair = await this.tokens.login(username, input.password);
      return sessionViewOf(pair, username);
    } catch (error) {
      throw classifyAuthError(error);
    }
  }

  /** 注册即登录：桥载荷 → RegisterCreate → 令牌登记会话。 */
  async register(input: { username: string; password: string }): Promise<SessionView> {
    const built = buildRegisterBody({
      username: input.username,
      password: input.password,
      fingerprint: this.fingerprint,
      platform: this.platform,
    });
    if (!built.ok) throw new AuthFailureError("invalid", built.message);
    try {
      const pair = await this.auth.register(built.body);
      this.tokens.adopt(pair);
      // buildRegisterBody 已将 trim 后账号写入 email/phone，会话视图沿用同一账号
      return sessionViewOf(pair, input.username.trim());
    } catch (error) {
      throw classifyAuthError(error);
    }
  }

  /** 注销：清本地会话并请求服务端吊销（未登录时幂等返回 0；吊销失败本地已清）。 */
  async logout(): Promise<number> {
    try {
      return await this.tokens.logout();
    } catch (error) {
      throw classifyAuthError(error);
    }
  }

  /** 是否已登录（当前内存会话存在）。 */
  signedIn(): boolean {
    return this.tokens.signedIn();
  }

  /** 访问令牌读取（联调验证受保护端点；业务通道接线经 TokenProvider 复用同一会话）。 */
  getToken(): Promise<string | null> {
    return this.tokens.getToken();
  }

  /** 业务通道 401/403 清会话（回退匿名；挂 CloudHttpClient.onUnauthorized 防死循环 401）。 */
  clearSession(): void {
    this.tokens.clearSession();
  }
}