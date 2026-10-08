/**
 * 会话令牌安全落盘（SP3-4 本地安全存储）：TokenStore 的加密持久化实现。
 *
 * 红线与语义：
 * - 明文不出进程：StoredSession JSON → KeyEncryptor（safeStorage/DPAPI）加密 → SecretStore（只存密文）；
 * - 空密文 = 已删除（SecretStore/KeyVault 既有约定）：save(null) 即清盘（登出/会话失效）；
 * - 读取全部 fail-safe：密文不可解 / JSON 非法 / 形状漂移 → null（回退未登录，不崩溃、不半信半疑）；
 * - scope 固定 `session.tokens`：与密钥库（key:<id>）空间隔离，互不覆盖。
 *
 * 本模块不依赖 electron（加密器经注入；node:test 可加载）。
 */
import type { StoredSession, TokenPair, TokenStore } from "./auth-client.ts";
import type { KeyEncryptor, SecretStore } from "../key-vault.ts";

/** 会话密文 scope（单一会话；登出即清）。 */
export const SESSION_SCOPE = "session.tokens";

function isTokenPair(value: unknown): value is TokenPair {
  if (typeof value !== "object" || value === null) return false;
  const pair = value as Record<string, unknown>;
  return (
    typeof pair["access_token"] === "string" &&
    typeof pair["refresh_token"] === "string" &&
    typeof pair["token_type"] === "string" &&
    typeof pair["expires_in"] === "number" &&
    typeof pair["refresh_expires_in"] === "number"
  );
}

/** 落盘载荷形状校验（防旧版本/损坏数据被当真）。 */
export function parseStoredSession(value: unknown): StoredSession | null {
  if (typeof value !== "object" || value === null) return null;
  const session = value as Record<string, unknown>;
  if (typeof session["username"] !== "string" || typeof session["issued_at_ms"] !== "number") return null;
  if (!isTokenPair(session["pair"])) return null;
  return {
    pair: session["pair"],
    issued_at_ms: session["issued_at_ms"],
    username: session["username"],
  };
}

/** 加密会话存储：TokenStore 实现（生产接 SqliteSecretStore + safeStorage 加密器）。 */
export function createEncryptedTokenStore(options: {
  secrets: SecretStore;
  encryptor: KeyEncryptor;
  /** 密文 scope（默认 session.tokens；测试可注入隔离空间）。 */
  scope?: string;
}): TokenStore {
  const scope = options.scope ?? SESSION_SCOPE;
  return {
    save(session: StoredSession | null): void {
      if (session === null) {
        options.secrets.set(scope, ""); // 空密文 = 删除
        return;
      }
      options.secrets.set(scope, options.encryptor.encrypt(JSON.stringify(session)));
    },
    load(): StoredSession | null {
      const ciphertext = options.secrets.get(scope);
      if (ciphertext === null || ciphertext === "") return null;
      try {
        return parseStoredSession(JSON.parse(options.encryptor.decrypt(ciphertext)));
      } catch {
        return null; // 密文损坏/密钥轮换/格式漂移：回退未登录（下次登录重写）
      }
    },
  };
}