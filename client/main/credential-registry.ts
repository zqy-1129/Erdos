import { randomUUID } from "node:crypto";
import { KeyVault } from "./key-vault.ts";
import { maskSecret } from "./secret-masker.ts";

const SCOPE = "client.credentials.v1";
export interface ModelCredential {
  id: string; alias: string; baseUrl: string; model: string; provider: string; key: string;
  status: "unknown" | "ok" | "fail";
}
export type CredentialView = Omit<ModelCredential, "key" | "provider"> & { masked: string; active: boolean };
interface RegistryState { activeId: string | null; entries: ModelCredential[] }

/** 拒绝凭据嵌入 URL；仅允许 HTTPS 和本机开发端点。错误不包含原始输入。 */
export function validateModelEndpoint(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048) throw new Error("模型服务地址无效");
  let url: URL;
  try { url = new URL(value.trim()); } catch { throw new Error("模型服务地址无效"); }
  if (url.username || url.password || url.search || url.hash ||
      (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) {
    throw new Error("模型服务地址需使用 HTTPS，本机调试可使用 HTTP；地址不得包含凭据或查询参数");
  }
  return url.href.replace(/\/+$/, "");
}
function text(value: unknown, max: number, label: string): string {
  if (typeof value !== "string" || !value.trim() || value.length > max || /[\r\n]/.test(value) || value.includes("\u0000")) {
    throw new Error(`${label}不能为空、超长或包含控制字符`);
  }
  return value.trim();
}

/** 密钥、模型坐标和激活状态一起加密保存，跨重启恢复；视图只返回脱敏凭据。 */
export class CredentialRegistry {
  private state: RegistryState;
  private readonly vault: KeyVault;
  private readonly id: () => string;
  constructor(vault: KeyVault, id: () => string = randomUUID) {
    this.vault = vault;
    this.id = id;
    try {
      const raw = vault.load(SCOPE);
      if (!raw) { this.state = { activeId: null, entries: [] }; return; }
      const parsed = JSON.parse(raw) as RegistryState;
      if (!Array.isArray(parsed.entries) || parsed.entries.length > 100 ||
          !parsed.entries.every(e => typeof e.id === "string" && e.id &&
            typeof e.alias === "string" && typeof e.model === "string" && e.model &&
            typeof e.provider === "string" && typeof e.key === "string" && e.key &&
            ["unknown", "ok", "fail"].includes(e.status) && validateModelEndpoint(e.baseUrl) === e.baseUrl) ||
          new Set(parsed.entries.map(e => e.id)).size !== parsed.entries.length ||
          (parsed.activeId !== null && !parsed.entries.some(e => e.id === parsed.activeId))) throw new Error();
      this.state = parsed;
    } catch { throw new Error("本地密钥配置无法解密，请恢复原 Windows 用户或备份后重新启动"); }
  }
  list(): CredentialView[] {
    return this.state.entries.map(({ key, provider: _provider, ...entry }) =>
      ({ ...entry, masked: maskSecret(key), active: entry.id === this.state.activeId }));
  }
  active(): ModelCredential | null {
    const entry = this.state.entries.find(e => e.id === this.state.activeId);
    return entry ? { ...entry } : null;
  }
  save(body: Record<string, unknown>): { ok: true; id: string } {
    if (this.state.entries.length >= 100) throw new Error("密钥配置数量已达上限，请先删除不使用的配置");
    const baseUrl = validateModelEndpoint(body.baseUrl);
    const entry: ModelCredential = {
      id: this.id(), alias: text(body.alias || "默认配置", 64, "配置名称"), baseUrl,
      model: text(body.model, 128, "模型名称"), key: text(body.key, 4096, "API Key"),
      provider: new URL(baseUrl).hostname === "dashscope.aliyuncs.com" ? "dashscope-compat" : "openai-compat",
      status: "unknown",
    };
    this.commit({ activeId: entry.id, entries: [...this.state.entries, entry] });
    return { ok: true, id: entry.id };
  }
  activate(id: string): void {
    if (!this.state.entries.some(e => e.id === id)) throw new Error("密钥配置不存在");
    this.commit({ ...this.state, activeId: id });
  }
  mark(status: ModelCredential["status"]): void {
    this.commit({ ...this.state, entries: this.state.entries.map(e => e.id === this.state.activeId ? { ...e, status } : e) });
  }
  remove(id: string): { ok: boolean; requeue: boolean } {
    if (!this.state.entries.some(e => e.id === id)) return { ok: false, requeue: false };
    const requeue = this.state.activeId === id;
    this.commit({ activeId: requeue ? null : this.state.activeId, entries: this.state.entries.filter(e => e.id !== id) });
    return { ok: true, requeue };
  }
  private commit(next: RegistryState): void {
    this.vault.save(SCOPE, JSON.stringify(next));
    this.state = next;
  }
}
