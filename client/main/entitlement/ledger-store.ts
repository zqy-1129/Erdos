/**
 * 离线流水账本加密落盘（SP3-4 第三批）：OfflineLedger 的安全存储实现。
 *
 * 红线与语义（与 session-store.ts / state-store.ts 同口径）：
 * - 全量条目 JSON → KeyEncryptor（safeStorage/DPAPI）加密 → SecretStore 只存密文；
 *   账本承载离线消耗的待补扣记账（本地余额门禁直接消费 pendingNet），明文落盘等同
 *   于把记账数据交给任何可写文件的进程/脚本 —— 与令牌同口径加密（纵深防御；
 *   本地记账非资金权威，最终以联网对账 DF-005 与服务端流水为准）；
 * - 空密文 = 已删除：clear() 即清盘（登录/注册/登出，会话级缓存，防跨账号待补扣残留）；
 * - scope 固定 `offline.ledger`：与密钥库（key:<id>）/会话（session.tokens）/权益
 *   （entitlement.state）空间隔离；
 * - 读取全程 fail-safe：密文不可解 / JSON 非法 / 形状漂移 → 空账本（不崩溃、不半信半疑；
 *   丢失的仅是本地待补扣记忆，联网对账以服务端为准，最坏情形是少量离线消费未被补扣）；
 * - 写穿：每次真实变更（追加 / 状态迁移 / 对账标记 / 清盘）立即整账本重加密落盘，
 *   且「落盘成功才算记账」——落盘失败回滚内存并抛错，杜绝「磁盘异常时内存已扣分、
 *   重启后重现可再次消费」的透支窗口；无变化调用（幂等命中 / 同态或终态迁移 /
 *   未知 exec_id / 重复标记）不产生写放大；
 * - 性能：条目数受离线用量约束（小），整账本重加密为 O(n)，n 为离线流水条数，可接受。
 *
 * 本模块不依赖 electron（加密器经注入；node:test 可加载）。
 */
import type { KeyEncryptor, SecretStore } from "../key-vault.ts";
import { InMemoryOfflineLedger, type OfflineLedger } from "./offline-ledger.ts";
import type { OfflineLedgerEntry } from "./types.ts";

/** 离线账本密文 scope（单账号会话缓存；登录/登出即清）。 */
export const OFFLINE_LEDGER_SCOPE = "offline.ledger";

/** 合法条目状态（枚举外视为形状漂移）。 */
const ENTRY_STATUSES = new Set(["reserved", "confirmed", "released"]);

function isEntry(value: unknown): value is OfflineLedgerEntry {
  if (typeof value !== "object" || value === null) return false;
  const entry = value as Record<string, unknown>;
  return (
    typeof entry["exec_id"] === "string" &&
    (typeof entry["task_id"] === "string" || entry["task_id"] === null) &&
    typeof entry["stage"] === "string" &&
    typeof entry["points"] === "number" &&
    Number.isInteger(entry["points"]) &&
    typeof entry["status"] === "string" &&
    ENTRY_STATUSES.has(entry["status"]) &&
    typeof entry["created_at"] === "string" &&
    !Number.isNaN(Date.parse(entry["created_at"])) &&
    typeof entry["uploaded"] === "boolean"
  );
}

/**
 * 落盘载荷形状校验（条目数组；exec_id 重复视为损坏 —— 恢复不得静默丢条目）。
 * 不合法 → null（回退空账本，按无待补扣处理）。
 */
export function parseStoredLedger(value: unknown): OfflineLedgerEntry[] | null {
  if (!Array.isArray(value)) return null;
  const entries: OfflineLedgerEntry[] = [];
  const seen = new Set<string>();
  for (const raw of value) {
    if (!isEntry(raw) || seen.has(raw.exec_id)) return null;
    seen.add(raw.exec_id);
    entries.push({ ...raw });
  }
  return entries;
}

/**
 * 加密离线账本：OfflineLedger 实现（生产接 SqliteSecretStore + safeStorage 加密器）。
 * 构造时水合（fail-safe）；此后语义委托 InMemoryOfflineLedger（单一事实来源），
 * 真实变更经 transact 写穿（落盘成功才算记账；失败回滚并抛错）。
 */
export function createEncryptedOfflineLedger(options: {
  secrets: SecretStore;
  encryptor: KeyEncryptor;
  /** 密文 scope（默认 offline.ledger；测试可注入隔离空间）。 */
  scope?: string;
  /** 条目的创建时钟（测试注入；缺省真实时间）。 */
  clock?: () => Date;
}): OfflineLedger {
  const scope = options.scope ?? OFFLINE_LEDGER_SCOPE;

  function restore(): OfflineLedgerEntry[] {
    const ciphertext = options.secrets.get(scope);
    if (ciphertext === null || ciphertext === "") return [];
    try {
      return parseStoredLedger(JSON.parse(options.encryptor.decrypt(ciphertext))) ?? [];
    } catch {
      return []; // 密文损坏/密钥轮换/格式漂移：回退空账本（下次记账重写）
    }
  }

  let inner = new InMemoryOfflineLedger(options.clock, restore());

  function persist(): void {
    options.secrets.set(scope, options.encryptor.encrypt(JSON.stringify(inner.allEntries())));
  }

  /**
   * 变更事务：先记内存、落盘成功后生效；落盘失败回滚内存并抛错（「落盘成功才算记账」，
   * 防磁盘异常时内存已扣分而重启后重现可再次消费窗口）。
   * 变更判定由 InMemoryOfflineLedger.revision() 单一来源给出（幂等/终态/已对账守卫在语义层）；
   * 无真实变更不写盘。
   */
  function transact<T>(mutate: () => T, write: () => void): T {
    const snapshot = inner.allEntries();
    const beforeRev = inner.revision();
    const result = mutate();
    if (inner.revision() === beforeRev) return result; // 无变更（幂等/同态/未知 ID）：不落盘
    try {
      write();
    } catch (error) {
      inner = new InMemoryOfflineLedger(options.clock, snapshot); // 回滚到变更前
      throw error;
    }
    return result;
  }

  return {
    append(entry: Omit<OfflineLedgerEntry, "status" | "created_at" | "uploaded">): OfflineLedgerEntry {
      return transact(() => inner.append(entry), persist);
    },
    get(execId: string): OfflineLedgerEntry | null {
      return inner.get(execId);
    },
    markStatus(execId: string, status: "confirmed" | "released"): OfflineLedgerEntry | null {
      return transact(() => inner.markStatus(execId, status), persist);
    },
    pendingNet(): number {
      return inner.pendingNet();
    },
    pendingItems(): OfflineLedgerEntry[] {
      return inner.pendingItems();
    },
    markUploaded(execIds: string[]): void {
      transact(() => inner.markUploaded(execIds), persist);
    },
    clear(): void {
      // 清盘为显式意图：无条件删除密文（含「密文损坏回退空账本后仍需清残留」的场景，
      // 见 ledger-store 头部 fail-safe 语义）；写失败回滚内存并抛错
      const snapshot = inner.allEntries();
      inner.clear();
      try {
        options.secrets.set(scope, ""); // 空密文 = 删除（与会话/权益存储同口径）
      } catch (error) {
        inner = new InMemoryOfflineLedger(options.clock, snapshot);
        throw error;
      }
    },
  };
}