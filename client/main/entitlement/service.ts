/**
 * 权益服务编排（SP3-3 核心）：快照获取→验签→落库；离线阶段许可与流水；
 * 联网对账（DF-005）批量补扣 + 快照重建；72h 宽限与欠费冻结。
 *
 * 依赖全部可注入（fetcher / keyResolver / uploader / clock / ledger / store），
 * 真实云端通道在 SP3-5 接入；本模块为纯逻辑，不依赖 Electron 运行时。
 */

import { InMemoryOfflineLedger, type OfflineLedger } from "./offline-ledger.ts";
import type {
  EntitlementSnapshot,
  EntitlementStatus,
  OfflineLedgerEntry,
  OfflineSyncItem,
  OfflineSyncResult,
} from "./types.ts";
import { EntitlementError } from "./types.ts";
import { canonicalBytes, verifySignature } from "./verify.ts";

/** 离线宽限默认时长（PRD：72h）。 */
export const DEFAULT_GRACE_MS = 72 * 3600 * 1000;

/** 本地离线阶段许可（SP3-3 本地签发记录，引擎离线宽限启动门禁消费）。 */
export interface LocalGrant {
  exec_id: string;
  stage: string;
  points: number;
  issued_at: string;
}

/** 云端快照拉取（SP3-5 接 /v1/entitlements/snapshot）。 */
export interface SnapshotFetcher {
  fetch(): Promise<EntitlementSnapshot>;
}

/** 按 key_version 解析服务端公钥（SPKI DER，SP3-5 接 /v1/auth/jwks）。 */
export interface PublicKeyResolver {
  resolve(keyVersion: string): Promise<Buffer | null>;
}

/** 离线批量补扣上报（SP3-5 接 /v1/points/offline-sync）。 */
export interface OfflineSyncUploader {
  upload(items: OfflineSyncItem[]): Promise<OfflineSyncResult>;
}

/** 快照落地存储（SP3-4 接本地 SQLite；内存实现为参考）。 */
export interface SnapshotStore {
  save(snapshot: EntitlementSnapshot): void;
  load(): EntitlementSnapshot | null;
}

export class InMemorySnapshotStore implements SnapshotStore {
  private current: EntitlementSnapshot | null = null;
  save(snapshot: EntitlementSnapshot): void {
    this.current = snapshot;
  }
  load(): EntitlementSnapshot | null {
    return this.current;
  }
}

export interface EntitlementDeps {
  fetcher: SnapshotFetcher;
  keyResolver: PublicKeyResolver;
  uploader: OfflineSyncUploader;
  ledger?: OfflineLedger;
  store?: SnapshotStore;
  /** ms 时间戳时钟（测试注入）。 */
  clock?: () => number;
  /** 离线宽限时长，默认 72h。 */
  graceMs?: number;
}

/** 纯函数：宽限判定（now - lastSyncAt 超过 graceMs 即宽限到期）。 */
export function graceExpired(
  lastSyncAtMs: number | null,
  nowMs: number,
  graceMs: number,
): boolean {
  return lastSyncAtMs !== null && nowMs - lastSyncAtMs > graceMs;
}

export class EntitlementService {
  private readonly deps: EntitlementDeps;
  private readonly ledger: OfflineLedger;
  private readonly store: SnapshotStore;
  private readonly clock: () => number;
  private readonly graceMs: number;
  /** 最近一次成功同步（拉取+验签+落库）时间。 */
  private lastSyncAtMs: number | null = null;
  /** 设备单调计数器：每次成功应用快照 +1（防回拨的本地佐证）。 */
  private deviceCounter = 0;
  /** 已接受快照的最近签发时间（防重放：新快照 issued_at 必须严格更晚）。 */
  private lastIssuedAtMs: number | null = null;

  constructor(deps: EntitlementDeps) {
    this.deps = deps;
    this.ledger = deps.ledger ?? new InMemoryOfflineLedger();
    this.store = deps.store ?? new InMemorySnapshotStore();
    this.clock = deps.clock ?? (() => Date.now());
    this.graceMs = deps.graceMs ?? DEFAULT_GRACE_MS;
  }

  /** 单测旁路：直接读取最近成功同步时间（断言宽限状态用）。 */
  lastSyncAt(): number | null {
    return this.lastSyncAtMs;
  }

  snapshot(): EntitlementSnapshot | null {
    return this.store.load();
  }

  counter(): number {
    return this.deviceCounter;
  }

  status(): EntitlementStatus {
    const snapshot = this.store.load();
    if (!snapshot) return "empty";
    if (snapshot.payload.frozen) return "frozen";
    const now = this.clock();
    if (this.lastSyncAtMs === null) {
      return "empty"; // 理论上不会落在（有快照必有 sync 时间）
    }
    return graceExpired(this.lastSyncAtMs, now, this.graceMs) ? "grace_expired" : "ready";
  }

  /** 剩余宽限毫秒（负数表示已超）。 */
  graceRemainingMs(): number | null {
    if (this.lastSyncAtMs === null) return null;
    return this.graceMs - (this.clock() - this.lastSyncAtMs);
  }

  /**
   * 拉取 → 验签 → 防回拨 → 落库。失败保持旧快照不变（离线可用性）。
   * 返回新快照；验签失败/公钥缺失/重放抛出对应 EntitlementError。
   */
  async refresh(): Promise<EntitlementSnapshot> {
    const snapshot = await this.deps.fetcher.fetch();
    const publicKey = await this.deps.keyResolver.resolve(snapshot.key_version);
    if (!publicKey) {
      throw new EntitlementError("SIGNATURE_INVALID", `未知密钥版本：${snapshot.key_version}`);
    }
    const ok = verifySignature(
      canonicalBytes(snapshot.payload),
      snapshot.signature,
      publicKey,
    );
    if (!ok) {
      throw new EntitlementError("SIGNATURE_INVALID", "快照验签失败（可能被篡改）");
    }
    this.apply(snapshot);
    return snapshot;
  }

  /** 应用已验证快照：防回拨 + 计数器 + 落库 + 重置宽限。 */
  private apply(snapshot: EntitlementSnapshot): void {
    // issued_at 已纳入签名载荷，防回拨判定基于签名保护的值（防篡改重放）
    const issuedAt = Date.parse(snapshot.payload.issued_at);
    if (Number.isNaN(issuedAt)) {
      throw new EntitlementError("SIGNATURE_INVALID", "快照签发时间非法");
    }
    if (this.lastIssuedAtMs !== null && issuedAt <= this.lastIssuedAtMs) {
      throw new EntitlementError("SNAPSHOT_REPLAY", "拒绝重放或过期快照");
    }
    this.lastIssuedAtMs = issuedAt;
    this.deviceCounter += 1;
    this.store.save(snapshot);
    this.lastSyncAtMs = this.clock();
  }

  /** 离线阶段许可：宽限/冻结/余额三门禁 → 记离线流水 → 返回本地许可。 */
  reserve(execId: string, taskId: string | null, stage: string, points: number): LocalGrant {
    if (!Number.isInteger(points) || points <= 0) {
      throw new EntitlementError("CONFLICT", "points 必须为正整数");
    }
    const snapshot = this.store.load();
    if (!snapshot) {
      throw new EntitlementError("NO_SNAPSHOT", "尚无有效权益快照");
    }
    if (snapshot.payload.frozen || this.status() === "frozen") {
      throw new EntitlementError("FROZEN", "账号欠费已冻结，无法启动新阶段");
    }
    if (this.status() === "grace_expired") {
      throw new EntitlementError("GRACE_EXPIRED", "离线宽限（72h）已超，请联网续期");
    }
    const balance = snapshot.payload.purchased_balance + snapshot.payload.monthly_balance;
    if (balance - this.ledger.pendingNet() < points) {
      throw new EntitlementError("INSUFFICIENT", "本地可用积分不足");
    }
    this.ledger.append({ exec_id: execId, task_id: taskId, stage, points });
    return {
      exec_id: execId,
      stage,
      points,
      issued_at: new Date(this.clock()).toISOString(),
    };
  }

  /** 确认离线消耗（终态，参与待补扣）。 */
  confirm(execId: string): OfflineLedgerEntry {
    const entry = this.ledger.markStatus(execId, "confirmed");
    if (!entry) throw new EntitlementError("NOT_FOUND", `流水不存在：${execId}`);
    return entry;
  }

  /** 取消/退还离线阶段（不参与待补扣）。 */
  release(execId: string): OfflineLedgerEntry {
    const entry = this.ledger.markStatus(execId, "released");
    if (!entry) throw new EntitlementError("NOT_FOUND", `流水不存在：${execId}`);
    return entry;
  }

  /**
   * 联网对账（DF-005）：批量上报离线流水 → 全部受理后标记 → 重建快照。
   * 无待补扣时仅刷新快照；上报失败原样抛出（保留 pending 供下轮重试）。
   */
  async reconcile(): Promise<{ sync: OfflineSyncResult | null; snapshot: EntitlementSnapshot }> {
    const pending = this.ledger.pendingItems();
    let sync: OfflineSyncResult | null = null;
    if (pending.length > 0) {
      const items = pending.map((e) => ({
        exec_id: e.exec_id,
        task_id: e.task_id,
        stage: e.stage,
        points: e.points,
      }));
      try {
        sync = await this.deps.uploader.upload(items);
      } catch (error) {
        throw new EntitlementError("UPLOAD_FAILED", `离线补扣上报失败：${String(error)}`);
      }
      this.ledger.markUploaded(items.map((i) => i.exec_id));
    }
    const snapshot = await this.refresh(); // 快照重建（含冻结态回读）
    return { sync, snapshot };
  }
}