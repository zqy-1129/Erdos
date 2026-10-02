/**
 * 密钥管家（SP3-2）：DPAPI/Keychain 加密抽象 + Key 密文落库 + stdin 注入。
 *
 * 红线（SP3-2 提示词）：
 * - Key 只以密文落库、解密后仅经 stdin 注入引擎；
 * - 禁止明文 Key 落盘或进日志。
 */

/** 密钥加密抽象层（Windows DPAPI / macOS Keychain 实现）。 */
export interface KeyEncryptor {
  encrypt(plaintext: string): string; // 密文（base64）
  decrypt(ciphertext: string): string; // 明文
}

/** 密钥库存储接口（SQLite 实现：只存密文）。 */
export interface SecretStore {
  get(scope: string): string | null; // 读取密文
  set(scope: string, ciphertext: string): void; // 写入密文
  has(scope: string): boolean;
}

/**
 * KeyVault：密钥管家。明文仅在内存中短暂存在（注入引擎时），
 * 落库只存密文，日志/遥测均脱敏。
 */
export class KeyVault {
  private readonly encryptor: KeyEncryptor;
  private readonly store: SecretStore;

  constructor(encryptor: KeyEncryptor, store: SecretStore) {
    this.encryptor = encryptor;
    this.store = store;
  }

  /** 保存 Key：加密后落库，返回是否成功。明文不落盘。 */
  save(scope: string, plaintext: string): void {
    const ciphertext = this.encryptor.encrypt(plaintext);
    this.store.set(scope, ciphertext);
  }

  /** 读取 Key：解密后返回明文（仅内存，调用方须立即注入引擎后丢弃）。 */
  load(scope: string): string | null {
    const ciphertext = this.store.get(scope);
    if (ciphertext === null) return null;
    return this.encryptor.decrypt(ciphertext);
  }

  /** 是否存在某 scope 的 Key。 */
  has(scope: string): boolean {
    return this.store.has(scope);
  }

  /** 删除 Key（密文）。 */
  remove(scope: string): void {
    this.store.set(scope, "");
  }
}

/** 内存存储实现（测试用；生产为 SQLite）。 */
export class InMemorySecretStore implements SecretStore {
  private readonly map = new Map<string, string>();

  get(scope: string): string | null {
    return this.map.get(scope) ?? null;
  }

  set(scope: string, ciphertext: string): void {
    if (ciphertext === "") {
      this.map.delete(scope);
    } else {
      this.map.set(scope, ciphertext);
    }
  }

  has(scope: string): boolean {
    return this.map.has(scope);
  }
}

/** XOR 加密实现（测试桩；生产用 DPAPI/Keychain）。 */
export class XorEncryptor implements KeyEncryptor {
  private readonly secretKey: string;

  constructor(secretKey: string = "erdos-test") {
    this.secretKey = secretKey;
  }

  private xor(input: string): string {
    const key = this.secretKey;
    let out = "";
    for (let i = 0; i < input.length; i++) {
      out += String.fromCharCode(input.charCodeAt(i) ^ key.charCodeAt(i % key.length));
    }
    return out;
  }

  encrypt(plaintext: string): string {
    return Buffer.from(this.xor(plaintext), "utf-8").toString("base64");
  }

  decrypt(ciphertext: string): string {
    return this.xor(Buffer.from(ciphertext, "base64").toString("utf-8"));
  }
}
