/**
 * 规范化字节序与 Ed25519 验签（SP3-3）。
 *
 * 与服务端 Ed25519LicenseSigner 及 contracts/snapshot.md 第 1 节严格对齐：
 * canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
 * 即：键按字典序、紧凑序列化、非 ASCII 转 \uXXXX 转义、UTF-8 编码。
 * 签名输出为 hex 字符串；公钥为 SPKI DER（按 key_version 从 JWKS 解析）。
 */

import { verify as edVerify } from "node:crypto";

/** JSON 字符串转义（对齐 Python json.dumps ensure_ascii=True 的 \uXXXX 形式）。 */
function escapeJsonString(value: string): string {
  // JSON.stringify 处理引号与控制字符；再对 >= 0x7F 的 UTF-16 单元补 \uXXXX（含代理对，与 Python 一致）
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (c) => {
    return "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0");
  });
}

/** 任意 JSON 值的规范化序列化（键排序 + 紧凑 + 非 ASCII 转义）。 */
export function canonical(value: unknown): string {
  if (value === null) return "null";
  switch (typeof value) {
    case "string":
      return escapeJsonString(value);
    case "boolean":
      return value ? "true" : "false";
    case "number":
      // 快照/许可 payload 仅含整数字段（金额/积分/时间戳），Python 整数序列化不带小数点
      if (!Number.isFinite(value) || !Number.isInteger(value)) {
        throw new Error(`规范化仅支持有限整数，收到：${value}`);
      }
      return String(value);
    case "object": {
      if (Array.isArray(value)) {
        return "[" + value.map(canonical).join(",") + "]";
      }
      const obj = value as Record<string, unknown>;
      const keys = Object.keys(obj).sort();
      return (
        "{" +
        keys.map((k) => escapeJsonString(k) + ":" + canonical(obj[k])).join(",") +
        "}"
      );
    }
    default:
      throw new Error(`不支持的规范化类型：${typeof value}`);
  }
}

/** payload 规范化字节（待签名字节）。 */
export function canonicalBytes(payload: object): Buffer {
  return Buffer.from(canonical(payload), "utf8");
}

/**
 * Ed25519 验签：签名 hex → Buffer；公钥 SPKI DER。
 * 任意异常（格式错误/密钥缺失/算法不匹配）一律返回 false，不抛出。
 */
export function verifySignature(
  canonicalPayloadBytes: Buffer,
  signatureHex: string,
  publicKeyDer: Buffer,
): boolean {
  try {
    const signature = Buffer.from(signatureHex, "hex");
    if (signature.length !== 64) return false;
    return edVerify(
      null,
      canonicalPayloadBytes,
      { key: publicKeyDer, format: "der", type: "spki" },
      signature,
    );
  } catch {
    return false;
  }
}