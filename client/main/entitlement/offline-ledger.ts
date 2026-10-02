/**
 * 离线流水账本（SP3-3：offline_ledger）。
 *
 * 追加式记账：同 exec_id 幂等；released（取消退还）不计入待补扣净值；
 * 联网对账（DF-005）将 pending 条目批量上报 /v1/points/offline-sync 后标记 uploaded。
 * 内存实现为参考实现；SQLite 落库在 SP3-4 本地数据栈接入时替换（接口稳定）。
 */

import type { OfflineLedgerEntry, OfflineSyncItem } from "./types.ts";

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
}

/** 内存实现（SP3-4 前作为客户端默认存储；时钟可注入便于测试）。 */
export class InMemoryOfflineLedger implements OfflineLedger {
  private readonly entries = new Map<string, OfflineLedgerEntry>();
  private readonly clock: () => Date;

  constructor(clock: () => Date = () => new Date()) {
    this.clock = clock;
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
    entry.status = status;
    return entry;
  }

  pendingNet(): number {
    return this.pendingItems().reduce((sum, e) => sum + e.points, 0);
  }

  pendingItems(): OfflineLedgerEntry[] {
    return [...this.entries.values()]
      .filter((e) => (e.status === "reserved" || e.status === "confirmed") && !e.uploaded)
      .sort((a, b) => a.created_at.localeCompare(b.created_at));
  }

  markUploaded(execIds: string[]): void {
    for (const execId of execIds) {
      const entry = this.entries.get(execId);
      if (entry && !entry.uploaded) entry.uploaded = true;
    }
  }

  /** 转上报条目（SP3-5 云端集成的 /v1/points/offline-sync 请求体）。 */
  toSyncItems(): OfflineSyncItem[] {
    return this.pendingItems().map((e) => ({
      exec_id: e.exec_id,
      task_id: e.task_id,
      stage: e.stage,
      points: e.points,
    }));
  }
}