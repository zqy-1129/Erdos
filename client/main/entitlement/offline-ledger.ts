/**
 * 离线流水账本（SP3-3：offline_ledger）。
 *
 * 追加式记账：同 exec_id 幂等；released（取消退还）不计入待补扣净值；
 * 联网对账（DF-005）将 pending 条目批量上报 /v1/points/offline-sync 后标记 uploaded。
 * 内存实现为语义参考 + 加密落盘的内部实现（SP3-4 第三批：见 ledger-store.ts，
 * 账本水合/序列化复用本类，避免两份语义漂移）。
 */

import type { OfflineLedgerEntry } from "./types.ts";

export interface OfflineLedger {
  /** 追加一条离线流水；同 exec_id 幂等（返回既有条目，冲突字段不覆盖）。 */
  append(entry: Omit<OfflineLedgerEntry, "status" | "created_at" | "uploaded">): OfflineLedgerEntry;
  get(execId: string): OfflineLedgerEntry | null;
  /** 状态迁移：reserved → confirmed / released（终态）。 */
  markStatus(execId: string, status: "confirmed" | "released"): OfflineLedgerEntry | null;
  /** 待补扣净值（reserved/confirmed 且未上报的点数合计）。 */
  pendingNet(): number;
  /** 待上报条目（按创建时间升序，对齐服务端批量对账逐条回执）。 */
  pendingItems(): OfflineLedgerEntry[];
  /** 上报成功后标记已对账。 */
  markUploaded(execIds: string[]): void;
  /** 清空账本（会话级缓存：登录/注册/登出时随账号切换清空，防旧账号待补扣残留）。 */
  clear(): void;
}

/**
 * 内存实现（SP3-4 前作为客户端默认存储；时钟可注入便于测试）。
 * 支持以既有条目水合（跨重启恢复，见 ledger-store.ts；形状校验由调用方负责）。
 */
export class InMemoryOfflineLedger implements OfflineLedger {
  private readonly entries = new Map<string, OfflineLedgerEntry>();
  private readonly clock: () => Date;
  /** 语义版本号：仅真实变更递增（落盘外壳的写穿判定依据，避免壳层重复变更规则）。 */
  private rev = 0;

  constructor(clock: () => Date = () => new Date(), initial: readonly OfflineLedgerEntry[] = []) {
    this.clock = clock;
    for (const entry of initial) {
      this.entries.set(entry.exec_id, { ...entry }); // 防御性复制：外部引用不参与内部状态
    }
  }

  append(entry: Omit<OfflineLedgerEntry, "status" | "created_at" | "uploaded">): OfflineLedgerEntry {
    const existing = this.entries.get(entry.exec_id);
    if (existing) {
      return existing; // 幂等：同 exec_id 返回既有流水
    }
    const full: OfflineLedgerEntry = {
      ...entry,
      status: "reserved",
      created_at: this.clock().toISOString(),
      uploaded: false,
    };
    this.entries.set(entry.exec_id, full);
    this.rev += 1;
    return full;
  }

  get(execId: string): OfflineLedgerEntry | null {
    return this.entries.get(execId) ?? null;
  }

  markStatus(execId: string, status: "confirmed" | "released"): OfflineLedgerEntry | null {
    const entry = this.entries.get(execId);
    if (!entry) return null;
    if (entry.status === "released") {
      return entry; // 终态不可再迁移
    }
    if (status === "released" && entry.uploaded) {
      return entry; // 已对账的消耗不可退还（服务端终态语义）
    }
    if (entry.status === status) {
      return entry; // 同态迁移：无变更
    }
    entry.status = status;
    this.rev += 1;
    return entry;
  }

  pendingNet(): number {
    return this.pendingItems().reduce((sum, e) => sum + e.points, 0);
  }

  pendingItems(): OfflineLedgerEntry[] {
    return [...this.entries.values()]
      .filter((e) => (e.status === "reserved" || e.status === "confirmed") && !e.uploaded)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map((entry) => ({ ...entry })); // 防御性复制：外部变更不得绕过写穿
  }

  markUploaded(execIds: string[]): void {
    for (const execId of execIds) {
      const entry = this.entries.get(execId);
      if (entry && !entry.uploaded) {
        entry.uploaded = true;
        this.rev += 1;
      }
    }
  }

  /** 语义版本号（仅真实变更递增；落盘外壳据此判定写穿，已含幂等/终态/已对账守卫）。 */
  revision(): number {
    return this.rev;
  }

  /** 全量条目快照（含已释放/已对账；落盘序列化用；插入序，防御性复制）。 */
  allEntries(): OfflineLedgerEntry[] {
    return [...this.entries.values()].map((entry) => ({ ...entry }));
  }

  clear(): void {
    if (this.entries.size > 0) this.rev += 1; // 空账本清盘无变更（落盘外壳不写盘）
    this.entries.clear();
  }
}