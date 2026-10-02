/**
 * SQLite user_version 迁移链（SP3-2）：空库逐版本升到最新，数据不丢。
 *
 * 对齐《数据模型设计》第 1/5/6 章表结构。
 * PRAGMA user_version 表示当前 schema 版本；迁移链按版本号顺序执行。
 */

/** 迁移步骤：version → 执行 SQL。 */
export interface Migration {
  version: number;
  sql: string[];
}

/** 迁移链：从空库逐版本升到最新。 */
export const MIGRATIONS: Migration[] = [
  {
    version: 1,
    sql: [
      // 本地留痕（第 6 章 audit_trail，只追加）
      `CREATE TABLE IF NOT EXISTS audit_trail (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        event_type TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}',
        ts TEXT NOT NULL
      )`,
      // 本地产物索引（第 6 章 artifact_index）
      `CREATE TABLE IF NOT EXISTS artifact_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL
      )`,
      // 密钥密文存储（第 5 章密钥管家，只存密文）
      `CREATE TABLE IF NOT EXISTS secrets (
        scope TEXT PRIMARY KEY,
        ciphertext TEXT NOT NULL
      )`,
      // 遥测 outbox（第 6 章，批量上报）
      `CREATE TABLE IF NOT EXISTS telemetry_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_name TEXT NOT NULL,
        distinct_id TEXT NOT NULL,
        props TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
      )`,
    ],
  },
  {
    version: 2,
    // SP3-5 遥测 SDK：outbox 行补齐上报必填字段（对齐 /v1/telemetry/events 载荷）
    sql: [
      `ALTER TABLE telemetry_outbox ADD COLUMN app_version TEXT NOT NULL DEFAULT ''`,
      `ALTER TABLE telemetry_outbox ADD COLUMN os TEXT NOT NULL DEFAULT ''`,
      `ALTER TABLE telemetry_outbox ADD COLUMN channel TEXT NOT NULL DEFAULT 'stable'`,
    ],
  },
];
export const LATEST_VERSION = MIGRATIONS[MIGRATIONS.length - 1].version;

/**
 * 迁移执行器接口：sqlite 连接抽象（node:sqlite 或 better-sqlite3 实现）。
 */
export interface SqliteLike {
  exec(sql: string): void;
  pragma(name: string): number;
}

/** 执行迁移链：空库逐版本升到最新，返回当前版本。 */
export function migrate(db: SqliteLike): number {
  const current = db.pragma("user_version");
  for (const migration of MIGRATIONS) {
    if (migration.version > current) {
      for (const sql of migration.sql) {
        db.exec(sql);
      }
      // 标记版本（SQLite PRAGMA user_version = N）
      db.exec(`PRAGMA user_version = ${migration.version}`);
    }
  }
  return db.pragma("user_version");
}
