/**
 * 平台加密器工厂（SP3-2 / SP3-4 共用）：safeStorage（DPAPI/Keychain）优先，
 * 不可用时拒绝保存凭据，禁止弱加密回退。
 *
 * 抽自 bridge.ts 内联实现：密钥库与本地安全存储（会话令牌）共用同一加密口径，
 * 避免两处实现漂移（明文永不落盘，只写密文）。
 */
import { safeStorage } from "electron";
import type { KeyEncryptor } from "./key-vault.ts";

/** safeStorage（DPAPI/Keychain）加密器：密文 base64。 */
export class SafeStorageEncryptor implements KeyEncryptor {
  encrypt(plaintext: string): string {
    return safeStorage.encryptString(plaintext).toString("base64");
  }
  decrypt(ciphertext: string): string {
    return safeStorage.decryptString(Buffer.from(ciphertext, "base64"));
  }
}

/** 平台安全存储不可用时拒绝操作，告警不含凭据。 */
export function createPlatformEncryptor(onWarn?: (message: string) => void): KeyEncryptor {
  if (safeStorage.isEncryptionAvailable()) return new SafeStorageEncryptor();
  onWarn?.("平台安全存储不可用，已拒绝保存凭据");
  throw new Error("平台安全存储不可用，请恢复 Windows 用户加密服务后重试");
}
