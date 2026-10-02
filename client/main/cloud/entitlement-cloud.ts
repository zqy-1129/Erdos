/**
 * 云端权益集成（SP3-5）：SP3-3 EntitlementService 的真实云端注入实现。
 *
 * - CloudSnapshotFetcher → GET /v1/entitlements/snapshot（含载荷形状校验）；
 * - CloudOfflineSyncUploader → POST /v1/points/offline-sync（DF-005 批量补扣）；
 * - createCloudEntitlementService → 装配上述 + JWKS 验签解析为 EntitlementService。
 */

import { EntitlementService, type SnapshotFetcher } from "../entitlement/service.ts";
import type {
  EntitlementPayload,
  EntitlementSnapshot,
  OfflineSyncItem,
  OfflineSyncResult,
} from "../entitlement/types.ts";
import { CloudHttpClient } from "./http.ts";
import { JwksKeyResolver } from "./jwks.ts";

function isPayload(value: unknown): value is EntitlementPayload {
  if (typeof value !== "object" || value === null) return false;
  const p = value as Record<string, unknown>;
  return (
    typeof p["subscribed"] === "boolean" &&
    (typeof p["sub_end_at"] === "string" || p["sub_end_at"] === null) &&
    typeof p["purchased_balance"] === "number" &&
    typeof p["monthly_balance"] === "number" &&
    typeof p["frozen"] === "boolean" &&
    typeof p["issued_at"] === "string"
  );
}

function parseSnapshot(value: unknown): EntitlementSnapshot {
  if (typeof value !== "object" || value === null) {
    throw new Error("快照响应形状非法");
  }
  const s = value as Record<string, unknown>;
  if (!isPayload(s["payload"]) || typeof s["signature"] !== "string" ||
      typeof s["key_version"] !== "string" || typeof s["issued_at"] !== "string") {
    throw new Error("快照响应形状非法");
  }
  return {
    payload: s["payload"] as EntitlementPayload,
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
    timeoutMs: options.timeoutMs,
    maxRetries: options.maxRetries,
  });
  return new EntitlementService({
    fetcher: new CloudSnapshotFetcher(http),
    keyResolver: new JwksKeyResolver(http),
    uploader: new CloudOfflineSyncUploader(http),
  });
}