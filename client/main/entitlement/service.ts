/**
 * 权益服务编排（SP3-3 核心）：快照获取→验签→落库；离线阶段许可与流水；
 * 联网对账（DF-005）批量补扣 + 快照重建；72h 宽限与欠费冻结。
 *
 * 依赖全部可注入（fetcher / keyResolver / uploader / clock / ledger / store），
 * 真实云端通道在 SP3-5 接入，本地状态落盘在 SP3-4 第二批接入（EntitlementStateStore）；
 * 本模块为纯逻辑，不依赖 Electron 运行时。
 */

import { InMemoryOfflineLedger, type OfflineLedger } from "./offline-ledger.ts";
import type {
  EntitlementSnapshot,
  EntitlementStatus,
  OfflineLedgerEntry,
  OfflineSyncItem,
  OfflineSyncResult,
} from "./types.ts";
import { EntitlementError, isEntitlementSnapshot } from "./types.ts";
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

/**
 * 权益本地状态（SP3-4 第二批）：快照与同步元数据同存同取。
 * 宽限基准（last_sync_at_ms）、防重放单调门（last_issued_at_ms）与设备计数器
 * 必须随快照一并跨重启延续，否则重启后宽限倒计时归零、重放门失效。
 */
export interface StoredEntitlementState {
  snapshot: EntitlementSnapshot;
  /** 最近一次成功同步（拉取+验签+落库）的本地时间 ms（72h 宽限基准）。 */
  last_sync_at_ms: number;
  /** 已接受快照的最近签发时间 ms（防重放：新快照 issued_at 必须严格更晚）。 */
  last_issued_at_ms: number;
  /** 设备单调计数器（每次成功应用快照 +1；防回拨的本地佐证）。 */
  device_counter: number;
}

/** 权益本地状态存储（SP3-4 接本地安全存储，见 state-store.ts；内存实现为参考）。 */
export interface EntitlementStateStore {
  save(state: StoredEntitlementState): void;
  load(): StoredEntitlementState | null;
  /** 清空本地状态（会话级缓存：登录/注册/登出即清，防跨账号离线回退泄漏）。 */
  clear(): void;
}

export class InMemoryEntitlementStateStore implements EntitlementStateStore {
  private current: StoredEntitlementState | null = null;
  save(state: StoredEntitlementState): void {
    this.current = state;
  }
  load(): StoredEntitlementState | null {
    return this.current;
  }
  clear(): void {
    this.current = null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** 落盘载荷形状校验（防旧版本/损坏数据被当真；不合法 → null，按未同步处理）。 */
export function parseStoredEntitlementState(value: unknown): StoredEntitlementState | null {
  if (!isRecord(value)) return null;
  const snapshot = value["snapshot"];
  if (!isEntitlementSnapshot(snapshot)) return null;
  const numbers = [value["last_sync_at_ms"], value["last_issued_at_ms"], value["device_counter"]];
  if (!numbers.every((n) => typeof n === "number" && Number.isFinite(n))) return null;
  return {
    snapshot,
    last_sync_at_ms: value["last_sync_at_ms"] as number,
    last_issued_at_ms: value["last_issued_at_ms"] as number,
    device_counter: value["device_counter"] as number,
  };
}

export interface EntitlementDeps {
  fetcher: SnapshotFetcher;
  keyResolver: PublicKeyResolver;
  uploader: OfflineSyncUploader;
  ledger?: OfflineLedger;
  /** 权益本地状态存储（缺省内存；SP3-4 第二批加密落盘接入后宽限跨重启延续）。 */
  store?: EntitlementStateStore;
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
  private readonly store: EntitlementStateStore;
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
    this.store = deps.store ?? new InMemoryEntitlementStateStore();
    this.clock = deps.clock ?? (() => Date.now());
    this.graceMs = deps.graceMs ?? DEFAULT_GRACE_MS;
    // 跨重启恢复（SP3-4 第二批）：宽限基准/防重放门/计数器与快照同源回填，
    // 缺失即为未同步（不回退假态）；损坏数据由存储实现回读时校验为 null
    const restored = this.store.load();
    if (restored) {
      this.lastSyncAtMs = restored.last_sync_at_ms;
      this.lastIssuedAtMs = restored.last_issued_at_ms;
      this.deviceCounter = restored.device_counter;
    }
  }

  /** 单测旁路：直接读取最近成功同步时间（断言宽限状态用）。 */
  lastSyncAt(): number | null {
    return this.lastSyncAtMs;
  }

  /**
   * 清空本地状态、离线流水与内存态（会话级缓存语义）：登录/注册/登出时由桥调用，
   * 防「换账号 + 离线回退」展示上一账号的快照/待补扣；清空后按未同步处理（需联网刷新重建）。
   */
  reset(): void {
    this.store.clear();
    this.ledger.clear();
    this.lastSyncAtMs = null;
    this.lastIssuedAtMs = null;
    this.deviceCounter = 0;
  }

  snapshot(): EntitlementSnapshot | null {
    return this.store.load()?.snapshot ?? null;
  }

  counter(): number {
    return this.deviceCounter;
  }

  status(): EntitlementStatus {
    const snapshot = this.snapshot();
    if (!snapshot) return "empty";
    if (snapshot.payload.frozen) return "frozen";
    const now = this.clock();
    if (this.lastSyncAtMs === null) {
      return "empty"; // 理论上不会落在（快照与同步时间同源落盘）
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

  /** 应用已验证快照：防回拨 + 计数器 + 落盘（写穿）+ 重置宽限。 */
  private apply(snapshot: EntitlementSnapshot): void {
    // issued_at 已纳入签名载荷，防回拨判定基于签名保护的值（防篡改重放）
    const issuedAt = Date.parse(snapshot.payload.issued_at);
    if (Number.isNaN(issuedAt)) {
      throw new EntitlementError("SIGNATURE_INVALID", "快照签发时间非法");
    }
    if (this.lastIssuedAtMs !== null && issuedAt <= this.lastIssuedAtMs) {
      throw new EntitlementError("SNAPSHOT_REPLAY", "拒绝重放或过期快照");
    }
    const state: StoredEntitlementState = {
      snapshot,
      last_sync_at_ms: this.clock(),
      last_issued_at_ms: issuedAt,
      device_counter: this.deviceCounter + 1,
    };
    // 写穿顺序：先落盘再提交内存态——存储异常时 refresh 抛错且状态保持旧值（下轮重试）
    this.store.save(state);
    this.lastIssuedAtMs = state.last_issued_at_ms;
    this.deviceCounter = state.device_counter;
    this.lastSyncAtMs = state.last_sync_at_ms;
  }

  /** 离线阶段许可：宽限/冻结/余额三门禁 → 记离线流水 → 返回本地许可。 */
  reserve(execId: string, taskId: string | null, stage: string, points: number): LocalGrant {
    if (!Number.isInteger(points) || points <= 0) {
      throw new EntitlementError("CONFLICT", "points 必须为正整数");
    }
    const snapshot = this.snapshot();
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