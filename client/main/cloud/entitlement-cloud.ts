/**
 * 云端权益集成（SP3-5）：SP3-3 EntitlementService 的真实云端注入实现。
 *
 * - CloudSnapshotFetcher → GET /v1/entitlements/snapshot（含载荷形状校验）；
 * - CloudOfflineSyncUploader → POST /v1/points/offline-sync（DF-005 批量补扣）；
 * - createCloudEntitlementService → 装配上述 + JWKS 验签解析为 EntitlementService。
 */

import { EntitlementService, type SnapshotFetcher, type EntitlementStateStore } from "../entitlement/service.ts";
import type { OfflineLedger } from "../entitlement/offline-ledger.ts";
import {
  isEntitlementPayload,
  type EntitlementSnapshot,
  type OfflineSyncItem,
  type OfflineSyncResult,
} from "../entitlement/types.ts";
import { CloudHttpClient, type FetchLike } from "./http.ts";
import { JwksKeyResolver } from "./jwks.ts";

function parseSnapshot(value: unknown): EntitlementSnapshot {
  if (typeof value !== "object" || value === null) {
    throw new Error("快照响应形状非法");
  }
  const s = value as Record<string, unknown>;
  if (!isEntitlementPayload(s["payload"]) || typeof s["signature"] !== "string" ||
      typeof s["key_version"] !== "string" || typeof s["issued_at"] !== "string") {
    throw new Error("快照响应形状非法");
  }
  return {
    payload: s["payload"] as EntitlementSnapshot["payload"],
    signature: s["signature"] as string,
    key_version: s["key_version"] as string,
    issued_at: s["issued_at"] as string,
  };
}

/** 云端快照拉取器（SP3-3 SnapshotFetcher 实现）。 */
export class CloudSnapshotFetcher implements SnapshotFetcher {
  private readonly http: CloudHttpClient;

  constructor(http: CloudHttpClient) {
    this.http = http;
  }

  async fetch(): Promise<EntitlementSnapshot> {
    return parseSnapshot(await this.http.get("/v1/entitlements/snapshot"));
  }
}

/** 云端离线补扣上报器（SP3-3 OfflineSyncUploader 实现，对齐服务端 OfflineSyncView）。 */
export class CloudOfflineSyncUploader {
  private readonly http: CloudHttpClient;

  constructor(http: CloudHttpClient) {
    this.http = http;
  }

  async upload(items: OfflineSyncItem[]): Promise<OfflineSyncResult> {
    const value = await this.http.post<Record<string, unknown>>("/v1/points/offline-sync", {
      items,
    });
    return {
      applied: Number(value["applied"] ?? 0),
      duplicate: Number(value["duplicate"] ?? 0),
      insufficient: Number(value["insufficient"] ?? 0),
      frozen: Boolean(value["frozen"] ?? false),
    };
  }
}

export interface CloudEntitlementOptions {
  baseUrl: string;
  /** 令牌提供者（SP3-5 登录态接入；缺省匿名）。 */
  getToken?: (() => Promise<string | null>) | undefined;
  /** 401/403 清会话回调（挂 SessionTokenProvider.clearSession 回退匿名）。 */
  onUnauthorized?: ((status: number) => void) | undefined;
  /** 权益本地状态存储（SP3-4 第二批：宽限/防重放门跨重启延续；缺省内存）。 */
  store?: EntitlementStateStore | null;
  /** 离线流水账本（SP3-4 第三批：待补扣跨重启延续；缺省内存）。 */
  ledger?: OfflineLedger | null;
  /** ms 时间戳时钟（与桥视图同源；缺省真实时钟）。 */
  clock?: () => number;
  /** 传输注入（测试/联调；透传 CloudHttpClient）。 */
  fetchImpl?: FetchLike;
  timeoutMs?: number;
  maxRetries?: number;
}

/** 装配云端权益服务：快照拉取（JWKS 验签）→ 离线许可/账本 → DF-005 批量补扣闭环。 */
export function createCloudEntitlementService(
  options: CloudEntitlementOptions,
): EntitlementService {
  const http = new CloudHttpClient({
    baseUrl: options.baseUrl,
    getToken: options.getToken,
    onUnauthorized: options.onUnauthorized,
    fetchImpl: options.fetchImpl,
    timeoutMs: options.timeoutMs,
    maxRetries: options.maxRetries,
  });
  return new EntitlementService({
    fetcher: new CloudSnapshotFetcher(http),
    keyResolver: new JwksKeyResolver(http),
    uploader: new CloudOfflineSyncUploader(http),
    store: options.store ?? undefined,
    ledger: options.ledger ?? undefined,
    clock: options.clock,
  });
}
