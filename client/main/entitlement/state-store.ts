/**
 * 权益本地状态加密落盘（SP3-4 第二批）：EntitlementStateStore 的安全存储实现。
 *
 * 红线与语义（与 cloud/session-store.ts 同口径）：
 * - StoredEntitlementState JSON → KeyEncryptor（safeStorage/DPAPI）加密 → SecretStore 只存密文；
 *   快照虽经服务端签名（可验完整性），但本地离线门禁（余额/冻结/宽限）直接消费落盘值，
 *   明文落盘等同于把门禁数据交给任何可写文件的进程/脚本 —— 故与令牌同口径加密（纵深防御；
 *   本地宽限门禁非资金权威，最终以联网对账 DF-005 与服务端快照为准）；
 * - 空密文 = 已删除：clear() 即清盘（登录/注册/登出，会话级缓存，防跨账号离线回退泄漏）；
 * - scope 固定 `entitlement.state`：与密钥库（key:<id>）/会话（session.tokens）空间隔离；
 * - 读取全程 fail-safe：密文不可解 / JSON 非法 / 形状漂移 → null（回退未同步，不崩溃、不半信半疑）。
 *
 * 本模块不依赖 electron（加密器经注入；node:test 可加载）。
 */
import type { KeyEncryptor, SecretStore } from "../key-vault.ts";
import {
  parseStoredEntitlementState,
  type EntitlementStateStore,
  type StoredEntitlementState,
} from "./service.ts";

/** 权益状态密文 scope（单账号会话缓存；登录/登出即清）。 */
export const ENTITLEMENT_SCOPE = "entitlement.state";

/** 加密权益状态存储：EntitlementStateStore 实现（生产接 SqliteSecretStore + safeStorage 加密器）。 */
export function createEncryptedEntitlementStateStore(options: {
  secrets: SecretStore;
  encryptor: KeyEncryptor;
  /** 密文 scope（默认 entitlement.state；测试可注入隔离空间）。 */
  scope?: string;
}): EntitlementStateStore {
  const scope = options.scope ?? ENTITLEMENT_SCOPE;
  return {
    save(state: StoredEntitlementState): void {
      options.secrets.set(scope, options.encryptor.encrypt(JSON.stringify(state)));
    },
    load(): StoredEntitlementState | null {
      const ciphertext = options.secrets.get(scope);
      if (ciphertext === null || ciphertext === "") return null;
      try {
        return parseStoredEntitlementState(JSON.parse(options.encryptor.decrypt(ciphertext)));
      } catch {
        return null; // 密文损坏/密钥轮换/格式漂移：回退未同步（下次联网刷新重写）
      }
    },
    clear(): void {
      options.secrets.set(scope, ""); // 空密文 = 删除
    },
  };
}