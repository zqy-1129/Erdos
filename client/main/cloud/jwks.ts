/**
 * JWKS 公钥解析（SP3-5）：/v1/auth/jwks → 按 kid 缓存 → OKP/Ed25519 → SPKI DER。
 *
 * 服务端发布形如 { keys: [{ kty:"OKP", crv:"Ed25519", kid, use:"sig", alg:"EdDSA", x }] }，
 * x 为 base64url 编码的 32 字节原始公钥；Node verify 需要 SPKI DER，此处按
 * RFC 8410 固定前缀拼装：302a300506032b6570032100 || raw32。
 */

import { CloudHttpClient } from "./http.ts";

export interface JwkKey {
  kty: string;
  crv?: string;
  kid: string;
  x?: string;
}

export interface JwksResponse {
  keys: JwkKey[];
}

/** Ed25519 公钥 SPKI DER 固定前缀（RFC 8410）。 */
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

/** OKP/Ed25519 公钥（base64url x）→ SPKI DER；格式不符返回 null。 */
export function okpPublicToSpkiDer(x: string): Buffer | null {
  try {
    const raw = Buffer.from(x, "base64url");
    if (raw.length !== 32) return null;
    return Buffer.concat([ED25519_SPKI_PREFIX, raw]);
  } catch {
    return null;
  }
}

/** 按 kid 解析并缓存 SPKI DER（kid 与公钥值双键缓存，轮换即失效）。 */
export class JwksKeyResolver {
  private readonly cache = new Map<string, Buffer>();
  private readonly http: CloudHttpClient;

  constructor(http: CloudHttpClient) {
    this.http = http;
  }

  async resolve(keyVersion: string): Promise<Buffer | null> {
    const hit = this.cache.get(keyVersion);
    if (hit) return hit;

    const jwks = await this.http.get<JwksResponse>("/v1/auth/jwks");
    for (const key of jwks.keys ?? []) {
      if (key.kty !== "OKP" || key.crv !== "Ed25519" || !key.x) continue;
      const der = okpPublicToSpkiDer(key.x);
      if (der) this.cache.set(key.kid, der);
    }
    return this.cache.get(keyVersion) ?? null;
  }
}