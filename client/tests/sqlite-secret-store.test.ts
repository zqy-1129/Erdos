/**
 * SQLite 密钥落库单测（F1/F3 收尾）：迁移就位、密文读写、**跨实例持久化**（落库语义）、
 * 删除语义（空密文=已删除）、UPSERT 覆盖、WAL 开关、工厂降级（驱动/路径不可用）。
 * 全程只接触密文（明文加密属 KeyVault + safeStorage 职责，另行测试）。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { createSqliteSecretStore, SqliteSecretStore } from "../main/sqlite-secret-store.ts";
import { LATEST_VERSION } from "../main/migrations.ts";

function makeDbPath(): { dir: string; dbPath: string } {
  const dir = mkdtempSync(join(tmpdir(), "erdos-secrets-"));
  return { dir, dbPath: join(dir, "erdos.db") };
}

describe("SqliteSecretStore 密钥落库（F1/F3 收尾）", () => {
  it("建库即迁移到最新版本，secrets 表就位", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const store = new SqliteSecretStore(dbPath);
      try {
        const probe = new DatabaseSync(dbPath);
        const version = (probe.prepare("PRAGMA user_version").get() as { user_version: number }).user_version;
        assert.equal(version, LATEST_VERSION, "空库应升到迁移链最新版本");
        const table = probe.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='secrets'").get();
        assert.ok(table, "secrets 表应存在");
        probe.close();
      } finally {
        store.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("密文读写往返：set/get/has（未知 scope 为 null/false）", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const store = new SqliteSecretStore(dbPath);
      try {
        assert.equal(store.get("k-unknown"), null);
        assert.equal(store.has("k-unknown"), false);
        store.set("k-1", "cipher-base64-1");
        assert.equal(store.get("k-1"), "cipher-base64-1");
        assert.equal(store.has("k-1"), true);
      } finally {
        store.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("跨实例持久化：重开连接仍可读取（落库而非内存）", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const first = new SqliteSecretStore(dbPath);
      first.set("k-1", "c-persisted");
      first.close();

      const second = new SqliteSecretStore(dbPath);
      try {
        assert.equal(second.get("k-1"), "c-persisted", "重启后密钥密文应保留");
        assert.equal(second.has("k-1"), true);
      } finally {
        second.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("删除语义：空密文=已删除（get null / has false），可重新写入恢复", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const store = new SqliteSecretStore(dbPath);
      try {
        store.set("k-1", "c1");
        store.set("k-1", ""); // KeyVault.remove 约定
        assert.equal(store.get("k-1"), null);
        assert.equal(store.has("k-1"), false);

        store.set("k-1", "c2");
        assert.equal(store.get("k-1"), "c2", "删除后可重新写入");
      } finally {
        store.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("UPSERT：同 scope 重复写入以最后一次为准", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const store = new SqliteSecretStore(dbPath);
      try {
        store.set("k-1", "old");
        store.set("k-1", "new");
        assert.equal(store.get("k-1"), "new");
        const probe = new DatabaseSync(dbPath);
        const count = (probe.prepare("SELECT COUNT(*) AS n FROM secrets WHERE scope='k-1'").get() as { n: number }).n;
        probe.close();
        assert.equal(count, 1, "同 scope 不应产生多行");
      } finally {
        store.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("WAL 模式已开启（客户端架构一致性约定）", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const store = new SqliteSecretStore(dbPath);
      store.close();
      const probe = new DatabaseSync(dbPath);
      const mode = (probe.prepare("PRAGMA journal_mode").get() as { journal_mode: string }).journal_mode;
      probe.close();
      assert.equal(mode.toLowerCase(), "wal");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("迁移幂等：重复构造不抛错且既有数据保留", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const first = new SqliteSecretStore(dbPath);
      first.set("k-1", "c1");
      first.close();

      const second = new SqliteSecretStore(dbPath);
      try {
        assert.equal(second.get("k-1"), "c1");
      } finally {
        second.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("工厂降级：路径不可用 → 返回 null 并告警（调用方回退内存）", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-secrets-"));
    try {
      const warnings: string[] = [];
      const store = createSqliteSecretStore(join(dir, "no-such-dir", "erdos.db"), (message) => warnings.push(message));
      assert.equal(store, null);
      assert.equal(warnings.length, 1);
      assert.match(warnings[0] ?? "", /回退内存存储/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("工厂正常路径：返回可用实例", () => {
    const { dir, dbPath } = makeDbPath();
    try {
      const store = createSqliteSecretStore(dbPath);
      assert.notEqual(store, null);
      store?.set("k-1", "c1");
      assert.equal(store?.get("k-1"), "c1");
      (store as SqliteSecretStore).close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});