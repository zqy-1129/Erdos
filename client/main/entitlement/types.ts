/**
 * 权益快照与离线宽限（SP3-3）公共类型。
 *
 * 对齐 contracts/snapshot.md 第 2 节：
 * payload = { subscribed, sub_end_at, purchased_balance, monthly_balance, frozen, issued_at }；
 * signature 为 hex 128 字符；issued_at（签名载荷内）为服务端签发时间（防回拨依据）。
 */

/** 快照负载（对应《数据模型设计》entitlement_snapshots.payload）。 */
export interface EntitlementPayload {
  subscribed: boolean;
  sub_end_at: string | null;
  purchased_balance: number;
  monthly_balance: number;
  frozen: boolean;
  /** 签发时间（ISO 8601，纳入签名载荷，防回拨/重放判定依据）。 */
  issued_at: string;
}

/** 服务端签发的权益快照（/v1/entitlements/snapshot 响应）。 */
export interface EntitlementSnapshot {
  payload: EntitlementPayload;
  signature: string;
  key_version: string;
  issued_at: string;
}

/**
 * 快照整体形状校验（服务端响应解析与本地落盘回读共用，防字段漂移被当真）。
 * 仅校验形状，不做验签：签名有效性属 EntitlementService.refresh 职责。
 */
export function isEntitlementSnapshot(value: unknown): value is EntitlementSnapshot {
  if (typeof value !== "object" || value === null) return false;
  const s = value as Record<string, unknown>;
  return (
    isEntitlementPayload(s["payload"]) &&
    typeof s["signature"] === "string" &&
    typeof s["key_version"] === "string" &&
    typeof s["issued_at"] === "string"
  );
}

/**
 * 载荷形状校验（服务端响应解析与本地落盘回读共用，防字段漂移被当真）。
 * 不校验签名：验签属 EntitlementService.refresh 职责（本地载荷仅在验签通过后落盘）。
 */
export function isEntitlementPayload(value: unknown): value is EntitlementPayload {
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

/** 客户端权益状态机。 */
export type EntitlementStatus =
  | "empty" // 尚无有效快照
  | "ready" // 有效快照 + 宽限内
  | "grace_expired" // 快照有效但离线宽限（72h）已超
  | "frozen"; // 服务端标记欠费冻结

/** 离线流水条目（SP3-3：offline_ledger；联网后批量补扣 DF-005）。 */
export type OfflineEntryStatus = "reserved" | "confirmed" | "released";

export interface OfflineLedgerEntry {
  exec_id: string;
  task_id: string | null;
  stage: string;
  points: number;
  status: OfflineEntryStatus;
  created_at: string;
  uploaded: boolean;
}

/** 上报 /v1/points/offline-sync 的单条（对齐服务端 OfflineSyncBody.items）。 */
export interface OfflineSyncItem {
  exec_id: string;
  task_id: string | null;
  stage: string;
  points: number;
}

/** /v1/points/offline-sync 响应（对齐服务端 OfflineSyncView 关键字段）。 */
export interface OfflineSyncResult {
  applied: number;
  duplicate: number;
  insufficient: number;
  frozen: boolean;
}

/** 业务错误码（SP3-3 客户端口径，与云端错误码语义对齐）。 */
export type EntitlementErrorCode =
  | "NO_SNAPSHOT"
  | "SIGNATURE_INVALID"
  | "SNAPSHOT_REPLAY"
  | "FROZEN"
  | "GRACE_EXPIRED"
  | "INSUFFICIENT"
  | "CONFLICT"
  | "NOT_FOUND"
  | "UPLOAD_FAILED";

export class EntitlementError extends Error {
  readonly code: EntitlementErrorCode;

  constructor(code: EntitlementErrorCode, message: string) {
    super(message);
    this.code = code;
    this.name = "EntitlementError";
  }
}