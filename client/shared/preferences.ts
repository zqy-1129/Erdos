/** 客户端偏好仅包含非敏感配置，不承载 API Key 或会话令牌。 */
export interface ClientPreferences { defaultModel: string; language: "zh-CN" }
export const DEFAULT_PREFERENCES: ClientPreferences = { defaultModel: "", language: "zh-CN" };

export function parsePreferences(value: unknown): ClientPreferences {
  if (!value || typeof value !== "object") throw new Error("偏好配置必须为对象");
  const body = value as Record<string, unknown>;
  if (typeof body.defaultModel !== "string" || body.defaultModel.length > 128 ||
      /[\r\n]/.test(body.defaultModel) || body.defaultModel.includes("\u0000") || body.language !== "zh-CN") {
    throw new Error("偏好配置无效：请填写模型名称并使用简体中文");
  }
  return { defaultModel: body.defaultModel.trim(), language: "zh-CN" };
}
