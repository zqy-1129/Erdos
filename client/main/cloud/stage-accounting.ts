import type { StageName } from "../../shared/ipc.ts";
import { CloudHttpClient } from "./http.ts";
import { CloudApiError } from "./envelope.ts";
import { JwksKeyResolver } from "./jwks.ts";
import { canonicalBytes, verifySignature } from "../entitlement/verify.ts";
import type { EntitlementService } from "../entitlement/service.ts";

export interface StageCharge {
  execId: string;
  taskId: string;
  stage: StageName;
  points: number;
  mode: "online" | "offline";
}
export interface StageAccountingPort {
  reserve(charge: StageCharge): Promise<StageCharge["mode"]>;
  revalidate(charge: StageCharge): Promise<void>;
  confirm(charge: StageCharge): Promise<void>;
  refund(charge: StageCharge): Promise<void>;
}

/** 只有传输故障可使用离线宽限；鉴权、余额、签名和响应形状错误不能降级。 */
export function isTransportFailure(error: unknown): boolean {
  return error instanceof TypeError ||
    (error instanceof Error && ["AbortError", "TimeoutError"].includes(error.name)) ||
    (error instanceof CloudApiError && (error.httpStatus === 0 || error.httpStatus >= 500));
}

/** 阶段预扣与许可验签。写请求结果不明时保留 execId，避免再走离线重复预扣。 */
export class CloudStageAccounting implements StageAccountingPort {
  private readonly http: CloudHttpClient;
  private readonly offline: EntitlementService;
  private readonly keys: Pick<JwksKeyResolver, "resolve">;
  private readonly clock: () => number;
  constructor(http: CloudHttpClient, offline: EntitlementService,
    keys: Pick<JwksKeyResolver, "resolve"> = new JwksKeyResolver(http), clock = Date.now) {
    this.http = http; this.offline = offline; this.keys = keys; this.clock = clock;
  }
  async reserve(charge: StageCharge): Promise<StageCharge["mode"]> {
    try { await this.http.get("/v1/points/balance"); }
    catch (error) {
      if (!isTransportFailure(error)) throw error;
      this.offline.reserve(charge.execId, charge.taskId, charge.stage, charge.points);
      return "offline";
    }
    return this.issueOnline(charge);
  }
  /** 重试/继续必须重新验签；已在线预扣的执行不再降级离线，避免双记账。 */
  async revalidate(charge: StageCharge): Promise<void> {
    if (charge.mode === "offline") {
      if (this.offline.status() !== "ready") throw new Error("离线权益已过期或被冻结，请联网核对");
      return;
    }
    await this.issueOnline(charge);
  }
  private async issueOnline(charge: StageCharge): Promise<"online"> {
    const raw = await this.http.post<Record<string, unknown>>("/v1/points/reserve", {
      exec_id: charge.execId, task_id: charge.taskId, stage: charge.stage, points: charge.points,
    });
    const grant = raw.grant as Record<string, unknown> | undefined;
    const balance = raw.balance as Record<string, unknown> | undefined;
    if (!grant || !balance || typeof balance.user_id !== "string" ||
        grant.exec_id !== charge.execId || grant.stage !== charge.stage || grant.points !== charge.points ||
        grant.status !== "active" || typeof grant.signature !== "string" || typeof grant.key_version !== "string" ||
        typeof grant.issued_at !== "string" || typeof grant.expires_at !== "string") {
      throw new Error("阶段许可响应无效，已拒绝启动");
    }
    const issued = Date.parse(grant.issued_at);
    const expires = Date.parse(grant.expires_at);
    if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > this.clock() + 60000 ||
        expires <= this.clock() || expires <= issued || balance.frozen !== false) {
      throw new Error("阶段许可已过期或账号被冻结，已拒绝启动");
    }
    const key = await this.keys.resolve(grant.key_version);
    const payload = { exec_id: charge.execId, user_id: balance.user_id, task_id: charge.taskId,
      stage: charge.stage, points: charge.points, issued_at: Math.floor(issued / 1000), expires_at: Math.floor(expires / 1000) };
    if (!key || !verifySignature(canonicalBytes(payload), grant.signature, key)) {
      throw new Error("阶段许可验签失败，已拒绝启动");
    }
    return "online";
  }
  async confirm(charge: StageCharge): Promise<void> {
    if (charge.mode === "offline") { this.offline.confirm(charge.execId); return; }
    const result = await this.http.post<{ status: string }>("/v1/points/confirm", { exec_id: charge.execId });
    if (result.status !== "confirmed") throw new Error("阶段确认响应无效，请重试对账");
  }
  async refund(charge: StageCharge): Promise<void> {
    if (charge.mode === "offline") { this.offline.release(charge.execId); return; }
    try {
      const result = await this.http.post<{ status: string }>("/v1/points/refund", { exec_id: charge.execId });
      if (result.status !== "refunded") throw new Error("阶段退还响应无效，请重试对账");
    } catch (error) {
      // 写请求从未到达服务端：查无此流水意味着没有需要退还的预扣。
      if (!(error instanceof CloudApiError && error.httpStatus === 404)) throw error;
    }
  }
}
