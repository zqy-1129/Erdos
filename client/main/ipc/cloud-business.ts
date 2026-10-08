/**
 * 云端业务通道接线（SP3-5 应用层）：权益快照 / 账单（订阅 + 余额 + 流水）→ 桥视图。
 *
 * - 权益：复用 SP3-5 云端适配层（createCloudEntitlementService：拉取 /v1/entitlements/snapshot
 *   → JWKS 验签 → 防回拨落库 → 72h 宽限），桥视图由本地快照派生；
 *   刷新失败时回退本地快照视图并标 stale（离线宽限跨重启，见 entitlement()）；
 * - 账单：/v1/billing/subscription（契约按 plan 维度，monthly/yearly 并行取后择取；
 *   null=免费版）+ /v1/points/balance（余额）+ /v1/points/ledger（流水页，时间倒序）；
 * - 401/403 → onUnauthorized（挂 SessionTokenProvider.clearSession，回退匿名防死循环），
 *   并归一为「登录已失效」可读提示（渲染层会话刷新走后续「失效下发」批次）。
 *
 * 本模块不依赖 electron（node:test 直接加载）；令牌经 getToken 注入，不出主进程。
 */
import path from "node:path";
import { CloudApiError } from "../cloud/envelope.ts";
import { CloudHttpClient, type FetchLike } from "../cloud/http.ts";
import { createCloudEntitlementService } from "../cloud/entitlement-cloud.ts";
import type { EntitlementService, EntitlementStateStore } from "../entitlement/service.ts";
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
  /** true=联网刷新失败、本次为本地快照视图（渲染层显示「同步失败」轻提示）。 */
  stale: boolean;
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

/**
 * 导出落盘回调（主进程接线为原生保存对话框 + 写盘；测试注入桩）。
 * 返回实际保存路径；用户取消时 canceled=true、path=null（不写盘、不报错）。
 */
export type ExportSaver = (
  suggestedName: string,
  content: string,
) => Promise<{ canceled: boolean; path: string | null }>;

/** 账单导出视图（渲染层 BillingExportView）。 */
export interface BillingExportView {
  /** 建议/实际文件名（取消时为建议名）。 */
  filename: string;
  /** 实际保存路径（用户取消或未落盘为 null）。 */
  savedPath: string | null;
  canceled: boolean;
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

/** 导出文件名时间戳（本地时区 YYYYMMDD-HHmm，便于用户区分多次导出）。 */
export function formatStamp(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
}

/** 已验证快照 → 权益视图（balance=购买余额+月度余额；宽限到期=最近同步+72h）。 */
export function entitlementViewOf(
  service: EntitlementService,
  nowMs: number,
): Omit<EntitlementView, "stale"> {
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
  /** 权益本地状态存储（SP3-4 第二批：宽限/防重放门跨重启延续；缺省内存）。 */
  entitlementStore?: EntitlementStateStore | null;
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
    this.clock = options.clock ?? (() => Date.now());
    this.http = new CloudHttpClient({
      baseUrl: options.baseUrl,
      getToken: options.getToken,
      onUnauthorized: options.onUnauthorized,
      fetchImpl: options.fetchImpl,
      timeoutMs: options.timeoutMs,
      maxRetries: options.maxRetries,
    });
    // 权益链路复用 SP3-5 适配层（独立客户端实例；注入同一令牌/清会话回调/本地状态存储；
    // 时钟与桥视图同源：宽限剩余与到期时刻必须基于同一时间基准计算）
    this.entitlementService = createCloudEntitlementService({
      baseUrl: options.baseUrl,
      getToken: options.getToken,
      onUnauthorized: options.onUnauthorized,
      store: options.entitlementStore ?? undefined,
      clock: this.clock,
      fetchImpl: options.fetchImpl,
      timeoutMs: options.timeoutMs,
      maxRetries: options.maxRetries,
    });
  }

  /**
   * 清空本地权益状态（会话级缓存语义）：BridgeBackend 在登录/注册/登出时调用，
   * 防跨账号离线回退展示上一账号快照；清空后需联网刷新重建。
   */
  resetEntitlement(): void {
    this.entitlementService.reset();
  }

  /**
   * 权益视图：联网刷新（拉取 → JWKS 验签 → 防回拨）成功后返回最新视图。
   * 刷新失败但有本地快照（含跨重启恢复的落盘快照）→ 返回本地视图并标 stale，
   * 保证断网启动仍可展示快照余额与宽限倒计时；无本地快照/会话失效（401/403）原样抛出。
   */
  async entitlement(): Promise<EntitlementView> {
    return this.guard(async () => {
      try {
        await this.entitlementService.refresh();
        return { ...entitlementViewOf(this.entitlementService, this.clock()), stale: false };
      } catch (error) {
        // 401/403：会话已由 onUnauthorized 清除，渲染层回登录页（不透出旧快照）
        if (error instanceof CloudApiError && (error.httpStatus === 401 || error.httpStatus === 403)) {
          throw error;
        }
        const local = entitlementViewOf(this.entitlementService, this.clock());
        if (local.status === "empty") throw error; // 无本地快照：无法回退（渲染层 stale 轻提示）
        // 可观测：回退不静默（含验签失败/重放等安全类错误的归因线索）
        const reason = error instanceof Error ? error.message : String(error);
        console.warn(`[client] 权益刷新失败，回退本地快照（stale）：${reason}`);
        return { ...local, stale: true };
      }
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
   * 积分流水 CSV 导出：GET /v1/points/ledger/export（裸 CSV，上限 200 条）
   * → 经注入的保存回调落盘（原生保存对话框由主进程接线方提供；取消不视为失败）。
   * 注：fetch 的 text() 解码会剥离响应开头的 BOM；契约要求 UTF-8 BOM（Excel 兼容），
   * 故落盘前补回（内容语义不变，仅恢复服务端发出的 BOM 前缀）。
   */
  async exportLedgerCsv(save: ExportSaver): Promise<BillingExportView> {
    const csv = await this.guard(() => this.http.getRaw("/v1/points/ledger/export"));
    const content = csv.startsWith("\ufeff") ? csv : `\ufeff${csv}`;
    const suggestedName = `erdos-ledger-${formatStamp(this.clock())}.csv`;
    let result: { canceled: boolean; path: string | null };
    try {
      result = await save(suggestedName, content);
    } catch (error) {
      // 落盘失败（磁盘/权限/对话框异常）归一为可读前缀（渲染层直接展示；经 IPC 不被裸前缀污染）
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(`导出保存失败：${reason}`, { cause: error });
    }
    return {
      filename: result.path ? path.basename(result.path) : suggestedName,
      savedPath: result.path,
      canceled: result.canceled,
    };
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