/**
 * 云端业务通道接线（SP3-5 应用层）：权益快照 / 账单（订阅 + 余额 + 流水）→ 桥视图。
 *
 * - 权益：复用 SP3-5 云端适配层（createCloudEntitlementService：拉取 /v1/entitlements/snapshot
 *   → JWKS 验签 → 防回拨落库 → 72h 宽限），桥视图由本地快照派生；刷新失败原样抛出，
 *   由渲染层标记 stale 并保留上次快照与倒计时（见 offline-banner.tsx）；
 * - 账单：/v1/billing/subscription（契约按 plan 维度，monthly/yearly 并行取后择取；
 *   null=免费版）+ /v1/points/balance（余额）+ /v1/points/ledger（流水页，时间倒序）；
 * - 401/403 → onUnauthorized（挂 SessionTokenProvider.clearSession，回退匿名防死循环），
 *   并归一为「登录已失效」可读提示（渲染层会话刷新走后续「失效下发」批次）。
 *
 * 本模块不依赖 electron（node:test 直接加载）；令牌经 getToken 注入，不出主进程。
 */
import { CloudApiError } from "../cloud/envelope.ts";
import { CloudHttpClient, type FetchLike } from "../cloud/http.ts";
import { createCloudEntitlementService } from "../cloud/entitlement-cloud.ts";
import type { EntitlementService } from "../entitlement/service.ts";
import type { EntitlementStatus } from "../entitlement/types.ts";

// ---------------------------------------------------------------------------
// 桥视图（渲染层同形；主进程归一，渲染层不感知云端信封）
// ---------------------------------------------------------------------------

/** 权益视图（渲染层 EntitlementView：断网横幅数据源）。 */
export interface EntitlementView {
  status: EntitlementStatus;
  balance: number;
  /** 宽限到期时刻（绝对 ms 时间戳；null=无快照）。 */
  graceDeadlineMs: number | null;
}

/** 账单总览（渲染层 BillingOverview）。 */
export interface BillingOverviewView {
  planName: string;
  subEndAt: string | null;
  pointsBalance: number;
}

/** 积分流水行（渲染层 BillingLedgerRow）。 */
export interface BillingLedgerRowView {
  ts: string;
  action: string;
  stage: string;
  points: number;
  taskId: string;
}

// ---------------------------------------------------------------------------
// 服务端载荷形状（对齐 v1 契约视图；仅取映射所需字段）
// ---------------------------------------------------------------------------

interface SubscriptionView {
  plan: string;
  status: string;
  end_at: string;
}

interface BalanceView {
  purchased_balance: number;
  monthly_balance: number;
}

export interface LedgerItem {
  exec_id: string;
  delta: number;
  kind: string;
  stage: string | null;
  created_at: string;
}

interface LedgerPage {
  items: LedgerItem[];
  total: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** 订阅解析（null=无该计划订阅）。 */
export function parseSubscription(value: unknown): SubscriptionView | null {
  if (value === null || value === undefined) return null;
  if (
    !isRecord(value) ||
    typeof value["plan"] !== "string" ||
    typeof value["status"] !== "string" ||
    typeof value["end_at"] !== "string"
  ) {
    throw new Error("订阅响应形状非法");
  }
  return { plan: value["plan"], status: value["status"], end_at: value["end_at"] };
}

/** 多计划订阅择取：active 优先，其次到期更晚者（全部为 null → 免费版语义）。 */
export function pickSubscription(subscriptions: (SubscriptionView | null)[]): SubscriptionView | null {
  const present = subscriptions.filter((sub): sub is SubscriptionView => sub !== null);
  if (present.length === 0) return null;
  const active = present.find((sub) => sub.status === "active");
  if (active) return active;
  return present.reduce((latest, sub) => (Date.parse(sub.end_at) > Date.parse(latest.end_at) ? sub : latest));
}

/** 余额解析（双余额冻结态契约字段校验）。 */
export function parseBalance(value: unknown): BalanceView {
  if (
    !isRecord(value) ||
    typeof value["purchased_balance"] !== "number" ||
    typeof value["monthly_balance"] !== "number"
  ) {
    throw new Error("余额响应形状非法");
  }
  return { purchased_balance: value["purchased_balance"], monthly_balance: value["monthly_balance"] };
}

/** 流水页解析（items 逐条校验，防响应漂移静默显示错误数据）。 */
export function parseLedgerPage(value: unknown): LedgerPage {
  if (!isRecord(value) || !Array.isArray(value["items"]) || typeof value["total"] !== "number") {
    throw new Error("流水响应形状非法");
  }
  const items = value["items"].map((raw): LedgerItem => {
    if (
      !isRecord(raw) ||
      typeof raw["exec_id"] !== "string" ||
      typeof raw["delta"] !== "number" ||
      typeof raw["kind"] !== "string" ||
      typeof raw["created_at"] !== "string" ||
      (raw["stage"] !== null && raw["stage"] !== undefined && typeof raw["stage"] !== "string")
    ) {
      throw new Error("流水响应形状非法");
    }
    return {
      exec_id: raw["exec_id"],
      delta: raw["delta"],
      kind: raw["kind"],
      stage: (raw["stage"] as string | null | undefined) ?? null,
      created_at: raw["created_at"],
    };
  });
  return { items, total: value["total"] };
}

// ---------------------------------------------------------------------------
// 视图映射（纯函数）
// ---------------------------------------------------------------------------

/** ISO 时间归一（服务端 datetime 序列化为 +00:00，渲染层按 Z 形态截断展示）。 */
export function normalizeIso(ts: string): string {
  return ts.replace("+00:00", "Z");
}

/** 已验证快照 → 权益视图（balance=购买余额+月度余额；宽限到期=最近同步+72h）。 */
export function entitlementViewOf(service: EntitlementService, nowMs: number): EntitlementView {
  const snapshot = service.snapshot();
  const remaining = service.graceRemainingMs();
  return {
    status: service.status(),
    balance: snapshot ? snapshot.payload.purchased_balance + snapshot.payload.monthly_balance : 0,
    graceDeadlineMs: remaining === null ? null : nowMs + remaining,
  };
}

/** 订阅 + 余额 → 账单总览（无订阅=免费版）。 */
export function billingOverviewOf(
  subscription: SubscriptionView | null,
  balance: BalanceView,
): BillingOverviewView {
  return {
    planName: subscription?.plan ?? "免费版",
    subEndAt: subscription ? normalizeIso(subscription.end_at) : null,
    pointsBalance: balance.purchased_balance + balance.monthly_balance,
  };
}

/**
 * 流水条目 → 桥行视图。
 * taskId 置空：服务端 LedgerView 不暴露任务号（exec_id 为幂等键，语义不同，
 * 不可冒充任务号）；契约补 task_id 后回填（见交付报告遗留项）。
 */
export function ledgerRowOf(item: LedgerItem): BillingLedgerRowView {
  return {
    ts: normalizeIso(item.created_at),
    action: item.kind,
    stage: item.stage ?? "",
    points: item.delta,
    taskId: "",
  };
}

// ---------------------------------------------------------------------------
// CloudBusinessBridge：桥业务通道 → 云端真实数据
// ---------------------------------------------------------------------------

export interface CloudBusinessOptions {
  baseUrl: string;
  /** 令牌提供者（登录态；缺省匿名）。 */
  getToken?: (() => Promise<string | null>) | undefined;
  /** 401/403 清会话回调（挂 SessionTokenProvider.clearSession）。 */
  onUnauthorized?: ((status: number) => void) | undefined;
  /** 传输注入（测试/联调）。 */
  fetchImpl?: FetchLike;
  timeoutMs?: number;
  maxRetries?: number;
  /** ms 时间戳时钟（测试注入）。 */
  clock?: () => number;
}

/** 云端业务桥：权益 / 账单真实数据（BridgeBackend 云模式 + 已登录时使用）。 */
export class CloudBusinessBridge {
  private readonly http: CloudHttpClient;
  private readonly entitlementService: EntitlementService;
  private readonly clock: () => number;

  constructor(options: CloudBusinessOptions) {
    this.http = new CloudHttpClient({
      baseUrl: options.baseUrl,
      getToken: options.getToken,
      onUnauthorized: options.onUnauthorized,
      fetchImpl: options.fetchImpl,
      timeoutMs: options.timeoutMs,
      maxRetries: options.maxRetries,
    });
    // 权益链路复用 SP3-5 适配层（独立客户端实例；注入同一令牌/清会话回调）
    this.entitlementService = createCloudEntitlementService({
      baseUrl: options.baseUrl,
      getToken: options.getToken,
      onUnauthorized: options.onUnauthorized,
      fetchImpl: options.fetchImpl,
      timeoutMs: options.timeoutMs,
      maxRetries: options.maxRetries,
    });
    this.clock = options.clock ?? (() => Date.now());
  }

  /**
   * 权益视图：联网刷新（拉取 → JWKS 验签 → 防回拨）成功后返回最新视图。
   * 刷新失败（网络/验签/重放）原样抛出：渲染层标记 stale，保留上次快照与宽限倒计时。
   */
  async entitlement(): Promise<EntitlementView> {
    return this.guard(async () => {
      await this.entitlementService.refresh();
      return entitlementViewOf(this.entitlementService, this.clock());
    });
  }

  /** 账单总览：订阅（monthly/yearly 并行，择取 active/更晚到期）+ 余额；任一失败即抛错。 */
  async billingOverview(): Promise<BillingOverviewView> {
    return this.guard(async () => {
      const [monthly, yearly, balance] = await Promise.all([
        this.http.get("/v1/billing/subscription?plan=monthly"),
        this.http.get("/v1/billing/subscription?plan=yearly"),
        this.http.get("/v1/points/balance"),
      ]);
      const subscription = pickSubscription([parseSubscription(monthly), parseSubscription(yearly)]);
      return billingOverviewOf(subscription, parseBalance(balance));
    });
  }

  /** 积分流水（时间倒序前 50 条 → 桥行视图）。 */
  async billingLedger(): Promise<BillingLedgerRowView[]> {
    return this.guard(async () => {
      const page = parseLedgerPage(await this.http.get("/v1/points/ledger?limit=50"));
      return page.items.map(ledgerRowOf);
    });
  }

  /**
   * 业务请求错误归一：401/403（主进程已清会话，见 onUnauthorized）→ 渲染层可读提示，
   * 避免直接暴露「未授权（HTTP 401）」；其余错误（网络/形态/服务端）原样抛出。
   */
  private async guard<T>(run: () => Promise<T>): Promise<T> {
    try {
      return await run();
    } catch (error) {
      if (error instanceof CloudApiError && (error.httpStatus === 401 || error.httpStatus === 403)) {
        throw new Error("登录已失效：请重新登录（本地会话已清除）", { cause: error });
      }
      throw error;
    }
  }
}