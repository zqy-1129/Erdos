/**
 * 日志脱敏中间件（SP3-2）：正则 + 白名单，零明文落盘。
 *
 * 红线（SP3-2 提示词）：
 * - Key 只以密文落库、解密后仅经 stdin 注入引擎；
 * - 日志与遥测统一脱敏（正则 + 白名单）；
 * - 禁止明文 Key 落盘或进日志。
 */

/** 常见 Key/Token 模式（正则）。 */
const KEY_PATTERNS: RegExp[] = [
  /sk-[A-Za-z0-9_-]{8,}/g, // OpenAI 风格
  /Bearer\s+[A-Za-z0-9._-]{8,}/g, // Bearer token
  /AKIA[0-9A-Z]{16}/g, // AWS Access Key
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}/g, // 裸 JWT（三段点分）
];

/** 日志对象中直接丢弃的敏感字段名子串（大小写不敏感）。 */
const SENSITIVE_FIELD_FRAGMENTS = [
  "key",
  "secret",
  "token",
  "password",
  "authorization",
  "credential",
  "cookie",
];

/** 脱敏单条 Key：仅保留前后 4 位。 */
export function maskSecret(secret: string): string {
  if (secret.length <= 8) return "****";
  return `${secret.slice(0, 4)}...${secret.slice(-4)}`;
}

/** 脱敏一段文本：匹配 Key 模式替换为脱敏形式。 */
export function maskText(text: string): string {
  let result = text;
  for (const pattern of KEY_PATTERNS) {
    result = result.replace(pattern, (m) => maskSecret(m));
  }
  return result;
}

/**
 * 脱敏日志字段对象：敏感字段名（key/secret/token/password/authorization/
 * credential/cookie 子串）直接丢弃，其余字段的字符串值做 Key 模式脱敏。
 * 零明文落日志。
 */
export function maskLogPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    const lower = key.toLowerCase();
    if (SENSITIVE_FIELD_FRAGMENTS.some((fragment) => lower.includes(fragment))) {
      continue; // 敏感字段直接丢弃（零明文）
    }
    if (typeof value === "string") {
      result[key] = maskText(value);
    } else {
      result[key] = value;
    }
  }
  return result;
}

/** 判断文本是否含明文 Key（零明文落盘校验用）。 */
export function containsSecret(text: string): boolean {
  return KEY_PATTERNS.some((p) => {
    p.lastIndex = 0;
    return p.test(text);
  });
}
