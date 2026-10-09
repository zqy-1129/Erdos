/**
 * 会话令牌加密落盘测试（SP3-4 本地安全存储）：
 * 只存密文、读写往返、跨实例持久化（模拟重启）、清盘语义、损坏/漂移全部 fail-safe。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { StoredSession } from "../main/cloud/auth-client.ts";
import { SESSION_SCOPE, createEncryptedTokenStore, parseStoredSession } from "../main/cloud/session-store.ts";
import { InMemorySecretStore, XorEncryptor } from "../main/key-vault.ts";

const SESSION: StoredSession = {
  pair: {
    access_token: "at-1",
    refresh_token: "rt-1",
    token_type: "Bearer",
    expires_in: 900,
    refresh_expires_in: 86_400,
  },
  issued_at_ms: 1_767_744_000_000,
  username: "alice@example.com",
};

function makeStore(secrets = new InMemorySecretStore()) {
  return {
    secrets,
    store: createEncryptedTokenStore({ secrets, encryptor: new XorEncryptor("test-key") }),
  };
}

describe("会话令牌加密落盘", () => {
  it("只存密文：读写往返不落明文，scope 为 session.tokens", () => {
    const { secrets, store } = makeStore();
    store.save(SESSION);
    const ciphertext = secrets.get(SESSION_SCOPE);
    assert.ok(ciphertext && ciphertext.length > 0);
    assert.ok(!ciphertext.includes("refresh_token"), "密文不得包含明文片段");
    assert.ok(!ciphertext.includes("alice@example.com"), "密文不得包含账号明文");
    assert.deepEqual(store.load(), SESSION);
  });

  it("跨实例持久化（模拟重启）：同一密文存储新实例可恢复会话", () => {
    const secrets = new InMemorySecretStore();
    makeStore(secrets).store.save(SESSION);
    const reopened = createEncryptedTokenStore({ secrets, encryptor: new XorEncryptor("test-key") });
    assert.deepEqual(reopened.load(), SESSION);
  });

  it("清盘语义：save(null) 删除密文，load 返回 null（登出/会话失效）", () => {
    const { secrets, store } = makeStore();
    store.save(SESSION);
    store.save(null);
    assert.equal(secrets.get(SESSION_SCOPE), null);
    assert.equal(store.load(), null);
  });

  it("fail-safe：密文损坏 / 密文非 JSON / 形状漂移 → 一律 null（回退未登录）", () => {
    const secrets = new InMemorySecretStore();
    const store = createEncryptedTokenStore({ secrets, encryptor: new XorEncryptor("test-key") });

    secrets.set(SESSION_SCOPE, "!!!not-base64-ciphertext###");
    assert.equal(store.load(), null);

    secrets.set(SESSION_SCOPE, new XorEncryptor("test-key").encrypt("{ 不是 JSON"));
    assert.equal(store.load(), null);

    secrets.set(SESSION_SCOPE, new XorEncryptor("test-key").encrypt(JSON.stringify({ username: "only-name" })));
    assert.equal(store.load(), null);

    assert.equal(store.load(), null);
  });

  it("scope 可注入且与密钥库隔离（互不覆盖）", () => {
    const { secrets, store } = makeStore();
    secrets.set("session.tokens.other", "another-ciphertext");
    store.save(SESSION);
    assert.equal(secrets.get("session.tokens.other"), "another-ciphertext");
    assert.deepEqual(store.load(), SESSION);

    const scoped = createEncryptedTokenStore({
      secrets,
      encryptor: new XorEncryptor("test-key"),
      scope: "custom.scope",
    });
    assert.equal(scoped.load(), null); // 不同 scope 不串读
  });

  it("载荷形状校验：合法/非法样例", () => {
    assert.deepEqual(parseStoredSession(SESSION), SESSION);
    assert.equal(parseStoredSession(null), null);
    assert.equal(parseStoredSession({}), null);
    assert.equal(
      parseStoredSession({ pair: { access_token: 1 }, issued_at_ms: 0, username: "a" }),
      null,
    );
  });
});