/**
 * 密钥管家与脱敏测试（SP3-2）：密钥全生命周期 + 零明文落盘 + 迁移链。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { InMemorySecretStore, KeyVault, XorEncryptor } from "../main/key-vault.ts";
import {
  containsSecret,
  maskLogPayload,
  maskSecret,
  maskText,
} from "../main/secret-masker.ts";
import { LATEST_VERSION, MIGRATIONS, migrate, type SqliteLike } from "../main/migrations.ts";

// ---------------------------------------------------------------------------
// 密钥管家：全生命周期 + 零明文落盘
// ---------------------------------------------------------------------------
class MemorySqlite implements SqliteLike {
  version = 0;
  exec(sql: string): void {
    const m = sql.match(/PRAGMA user_version\s*=\s*(\d+)/i);
    if (m) {
      this.version = Number(m[1]);
    }
  }
  pragma(name: string): number {
    if (name === "user_version") return this.version;
    return 0;
  }
}

describe("KeyVault 密钥管家", () => {
  it("密钥全生命周期：save → has → load → remove", () => {
    const store = new InMemorySecretStore();
    const vault = new KeyVault(new XorEncryptor(), store);

    vault.save("openai", "sk-test-abcdef123456");
    assert.equal(vault.has("openai"), true);

    const loaded = vault.load("openai");
    assert.equal(loaded, "sk-test-abcdef123456");

    vault.remove("openai");
    assert.equal(vault.has("openai"), false);
    assert.equal(vault.load("openai"), null); // 删除后读取为 null（空密文=已删除语义）
  });

  it("零明文落盘：store 中只有密文，无明文 Key", () => {
    const store = new InMemorySecretStore();
    const vault = new KeyVault(new XorEncryptor(), store);
    const plaintext = "sk-secret-plaintext-value";

    vault.save("openai", plaintext);

    // store 中只存密文（base64），不含明文
    const ciphertext = store.get("openai")!;
    assert.ok(ciphertext !== plaintext);
    assert.equal(containsSecret(ciphertext), false);
  });
});

// ---------------------------------------------------------------------------
// 日志脱敏
// ---------------------------------------------------------------------------
describe("SecretMasker 日志脱敏", () => {
  it("maskSecret 仅保留前后 4 位", () => {
    assert.equal(maskSecret("sk-abcdefgh12345678"), "sk-a...5678");
    assert.equal(maskSecret("short"), "****");
  });

  it("maskText 匹配 Key 模式替换", () => {
    const text = "call with sk-abcdefgh12345678 failed";
    const masked = maskText(text);
    assert.equal(containsSecret(masked), false);
    assert.ok(masked.includes("sk-a...5678"));
  });

  it("maskLogPayload 丢弃 Key 字段，白名单保留", () => {
    const payload = {
      page: "home",
      api_key: "sk-secret-abcdef123456",
      message: "error sk-abcdefgh12345678",
    };
    const masked = maskLogPayload(payload);
    assert.equal("api_key" in masked, false); // Key 字段丢弃
    assert.equal(masked.page, "home"); // 白名单字段保留
    assert.equal(containsSecret(String(masked.message)), false);
  });

  it("maskLogPayload 丢弃 token/password/authorization 变体字段", () => {
    const payload = {
      page: "settings",
      refresh_token: "rt-xxx",
      Password: "p@ss",
      Authorization: "Bearer abc.def.ghi",
      note: "ok",
    };
    const masked = maskLogPayload(payload);
    assert.equal("refresh_token" in masked, false);
    assert.equal("Password" in masked, false);
    assert.equal("Authorization" in masked, false);
    assert.equal(masked.note, "ok");
  });

  it("maskText 脱敏裸 JWT", () => {
    const text = "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-part-12345 leaked";
    const masked = maskText(text);
    assert.equal(containsSecret(masked), false);
    assert.ok(!masked.includes("eyJhbGciOiJIUzI1NiJ9"));
  });
});

// ---------------------------------------------------------------------------
// SQLite 迁移链
// ---------------------------------------------------------------------------
describe("Migrations 迁移链", () => {
  it("空库逐版本升到最新", () => {
    const db = new MemorySqlite();
    const version = migrate(db);
    assert.equal(version, LATEST_VERSION);
    assert.equal(db.version, LATEST_VERSION);
  });

  it("迁移链版本号连续递增", () => {
    for (let i = 0; i < MIGRATIONS.length; i++) {
      assert.equal(MIGRATIONS[i].version, i + 1);
    }
  });

  it("迁移包含密钥密文表与遥测 outbox 表", () => {
    const allSql = MIGRATIONS.flatMap((m) => m.sql).join("\n");
    assert.ok(allSql.includes("secrets")); // 密钥密文表
    assert.ok(allSql.includes("telemetry_outbox")); // 遥测 outbox
    assert.ok(allSql.includes("audit_trail")); // 留痕表（只追加）
  });
});
