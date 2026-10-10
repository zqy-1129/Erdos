import { CloudHttpClient } from "./http.ts";

export interface CloudContentItem {
  id: string;
  kind: "template" | "case";
  title: string;
  tags: string[];
  referenceOnly: boolean;
  complianceNote?: string;
  ossKey: string;
  sha256: string;
}
export function parseContent(value: unknown, kind: CloudContentItem["kind"]): CloudContentItem {
  if (!value || typeof value !== "object") throw new Error("内容响应形状无效");
  const data = value as Record<string, unknown>;
  if (typeof data.business_id !== "string" || !data.business_id || typeof data.oss_key !== "string" ||
      !data.oss_key || typeof data.sha256 !== "string" || !/^[a-f0-9]{64}$/i.test(data.sha256)) throw new Error("内容索引或文件校验值无效");
  if (kind === "template" && (typeof data.competition !== "string" || !["latex", "docx"].includes(String(data.format)))) throw new Error("模板格式无效");
  if (kind === "case" && (typeof data.title !== "string" || !data.title || typeof data.compliance_note !== "string" || !data.compliance_note.trim())) throw new Error("案例缺少标题或合规说明");
  const tags = kind === "case" ? data.method_tags : [data.competition, data.format];
  if (tags !== undefined && (!Array.isArray(tags) || !tags.every(t => typeof t === "string"))) throw new Error("内容标签无效");
  return { id: `${kind}:${data.business_id}`, kind, title: kind === "case" ? String(data.title) : `${data.competition} ${data.format} 模板`,
    tags: (tags ?? []) as string[], referenceOnly: kind === "case", ossKey: data.oss_key, sha256: data.sha256,
    ...(kind === "case" ? { complianceNote: String(data.compliance_note) } : {}) };
}

/** 内容请求经过云端权益校验；未取得授权时不会回退演示列表。 */
export class CloudContentClient {
  private readonly http: CloudHttpClient;
  constructor(http: CloudHttpClient) { this.http = http; }
  async list(): Promise<CloudContentItem[]> {
    const lists = await Promise.all([this.http.get<unknown>("/v1/content/templates"), this.http.get<unknown>("/v1/content/cases")]);
    if (!lists.every(Array.isArray)) throw new Error("内容列表响应形状无效");
    return [...(lists[0] as unknown[]).map(v => parseContent(v, "template")), ...(lists[1] as unknown[]).map(v => parseContent(v, "case"))];
  }
}
