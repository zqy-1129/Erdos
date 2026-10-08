/**
 * SQLite 密钥密文落库（SP3-2 / F1-F3 收尾项）：SecretStore 的持久化实现。
 *
 * 驱动选型（DEC-003 版本锁定纪律）：**node:sqlite** —— Electron 37 内置 Node 22.21 已提供，
 * 零原生依赖，直接规避 better-sqlite3 的 Electron ABI 重编译风险（方案 §12 风险 3）。
 * 注意：Node 22 会打印 ExperimentalWarning（功能性提示，不影响使用）；CI（Node 24）无此告警。
 *
 * 契约：
 * - 表结构 = migrations.ts v1 的 secrets(scope TEXT PRIMARY KEY, ciphertext TEXT NOT NULL)，只存密文；
 * - 一致性 = WAL + busy_timeout=5000（客户端架构「本地数据栈」一致性约定）；
 * - 删除语义 = 空密文视为已删除（KeyVault.remove 约定）：get 返回 null、has 为 false；
 * - 明文永不落库（加密由 KeyVault + safeStorage/DPAPI 完成，本模块只接触密文）。
 */
import { DatabaseSync } from "node:sqlite";
import { migrate, type SqliteLike } from "./migrations.ts";
import type { SecretStore } from "./key-vault.ts";

/**
 * DatabaseSync → migrations.SqliteLike 适配。
 * pragma 名称仅由迁移链内部传入（编译期常量 "user_version"），无注入面。
 */
function asSqliteLike(db: DatabaseSync): SqliteLike {
  return {
    exec: (sql: string): void => {
      db.exec(sql);
    },
    pragma: (name: string): number => {
      const row = db.prepare(`PRAGMA ${name}`).get() as Record<string, number> | undefined;
      return row?.[name] ?? 0;
    },
  };
}

/** 密钥密文 SQLite 存储（只存密文；读写均为 scope 粒度）。 */
export class SqliteSecretStore implements SecretStore {
  private readonly db: DatabaseSync;

  constructor(dbPath: string) {
    this.db = new DatabaseSync(dbPath);
    // WAL：主进程与引擎/后续模块并发读；busy_timeout：写冲突等待而非立即报错
    this.db.exec("PRAGMA journal_mode = WAL");
    this.db.exec("PRAGMA busy_timeout = 5000");
    // 空库逐版本迁移到最新（幂等；冲突/既有版本自动跳过）
    migrate(asSqliteLike(this.db));
  }

  /** 读取密文；空密文（已删除）按 null 处理。 */
  get(scope: string): string | null {
    const row = this.db.prepare("SELECT ciphertext FROM secrets WHERE scope = ?").get(scope) as
      | { ciphertext?: string }
      | undefined;
    const ciphertext = row?.ciphertext ?? "";
    return ciphertext === "" ? null : ciphertext;
  }

  /** 写入密文（UPSERT）；空串 = 删除该 scope（KeyVault.remove 语义）。 */
  set(scope: string, ciphertext: string): void {
    if (ciphertext === "") {
      this.db.prepare("DELETE FROM secrets WHERE scope = ?").run(scope);
      return;
    }
    this.db
      .prepare(
        "INSERT INTO secrets (scope, ciphertext) VALUES (?, ?) " +
          "ON CONFLICT(scope) DO UPDATE SET ciphertext = excluded.ciphertext",
      )
      .run(scope, ciphertext);
  }

  has(scope: string): boolean {
    return this.get(scope) !== null;
  }

  /** 关闭连接（应用退出/测试清理用）。 */
  close(): void {
    this.db.close();
  }
}

/**
 * 构造工厂：驱动/路径不可用（旧 Node、目录缺失等）时返回 null，
 * 由调用方回退内存实现并记日志（绝不静默丢失「落库」语义之外的能力）。
 */
export function createSqliteSecretStore(dbPath: string, onWarn?: (message: string) => void): SecretStore | null {
  try {
    return new SqliteSecretStore(dbPath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    onWarn?.(`SQLite 密钥落库不可用，回退内存存储（重启后 Key 需重录）：${message}`);
    return null;
  }
}