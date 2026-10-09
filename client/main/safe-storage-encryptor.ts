/**
 * 平台加密器工厂（SP3-2 / SP3-4 共用）：safeStorage（DPAPI/Keychain）优先，
 * 不可用时回退 XorEncryptor（仅开发期，调用方可记告警）。
 *
 * 抽自 bridge.ts 内联实现：密钥库与本地安全存储（会话令牌）共用同一加密口径，
 * 避免两处实现漂移（明文永不落盘，只写密文）。
 */
import { safeStorage } from "electron";
import { XorEncryptor, type KeyEncryptor } from "./key-vault.ts";

/** safeStorage（DPAPI/Keychain）加密器：密文 base64。 */
export class SafeStorageEncryptor implements KeyEncryptor {
  encrypt(plaintext: string): string {
    return safeStorage.encryptString(plaintext).toString("base64");
  }
  decrypt(ciphertext: string): string {
    return safeStorage.decryptString(Buffer.from(ciphertext, "base64"));
  }
}

/** 平台可用加密器；不可用时回退 Xor（开发期），onWarn 记录降级。 */
export function createPlatformEncryptor(onWarn?: (message: string) => void): KeyEncryptor {
  if (safeStorage.isEncryptionAvailable()) return new SafeStorageEncryptor();
  onWarn?.("safeStorage 不可用：加密回退 Xor（仅开发期，打包前需确保平台加密可用）");
  return new XorEncryptor();
}