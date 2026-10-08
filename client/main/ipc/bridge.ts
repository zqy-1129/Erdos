/**
 * 主进程桥后端（FE-PRELOAD W4）：业务通道的 ipcMain.handle 分发实现。
 *
 * 开发期演示数据（云端集成 SP3-5 落地前）；keys → KeyVault（safeStorage 加密），
 * telemetry → TelemetrySdk（白名单 + 隐私过滤）。明文 Key 永不出主进程。
 *
 * keysTest 真实化（FE-TOOLUI W12）：经注入的引擎探测函数走 provider_test
 * （见 main/ipc/key-probe.ts）；引擎未接线/Web 环境返回 unverified，不冒充连通。
 */
import { safeStorage } from "electron";
import { BRIDGE_CHANNELS } from "../../shared/bridge-channels.ts";
import { KeyVault, InMemorySecretStore, XorEncryptor, type KeyEncryptor, type SecretStore } from "../key-vault.ts";
import { maskSecret } from "../secret-masker.ts";
import { TelemetrySdk, type TelemetryUploader, type UploadResult } from "../telemetry/sdk.ts";
import { keysTestOutcome, type KeyTestOutcome, type ProbeFn } from "./key-probe.ts";

const STAGES = ["analysis", "modeling", "solving", "writing"];

const DEMO_CONTENT = [
  { id: "t1", kind: "template", title: "CUMCM 官方论文模板", tags: ["格式", "国赛"], referenceOnly: false },
  { id: "t2", kind: "template", title: "MCM 官方格式模板", tags: ["格式", "美赛"], referenceOnly: false },
  { id: "c1", kind: "case", title: "2024 A 题一等奖论文（节选）", tags: ["优化", "评价"], referenceOnly: true },
  { id: "c2", kind: "case", title: "2019 C 题机场出租车思路", tags: ["机理", "预测"], referenceOnly: true },
];

const DEMO_HISTORY = [
  { taskId: "t-1001", title: "示例题：嫦娥三号软着陆", status: "writing", updatedAt: "2026-10-02T18:00:00Z", resumable: true },
  { taskId: "t-1002", title: "2024 C 题打磨", status: "done", updatedAt: "2026-10-01T15:30:00Z", resumable: false },
];

/** safeStorage（DPAPI/Keychain）加密器；不可用时回退 XorEncryptor（开发期）。 */
class SafeStorageEncryptor implements KeyEncryptor {
  encrypt(plaintext: string): string {
    return safeStorage.encryptString(plaintext).toString("base64");
  }
  decrypt(ciphertext: string): string {
    return safeStorage.decryptString(Buffer.from(ciphertext, "base64"));
  }
}

/** 桥后端构造选项（FE-KEYIN 落库：密钥密文存储注入）。 */
export interface BridgeBackendOptions {
  /**
   * 密钥密文存储（通常为 SqliteSecretStore，见 main/sqlite-secret-store.ts）。
   * 未提供或为 null（驱动不可用降级路径）→ 回退 InMemorySecretStore（重启后需重录 Key）。
   */
  secretStore?: SecretStore | null;
}

export class BridgeBackend {
  private readonly keyVault: KeyVault;
  private readonly keyMetas = new Map<string, { alias: string; baseUrl: string; masked: string; status: string }>();
  private readonly telemetry: TelemetrySdk;
  private sessionUsername: string | null = null;
  private activeKeyId: string | null = null;
  /** 引擎探测函数（FE-KEYIN/W12 接线：EngineHost.reloadKey → provider_test）。 */
  private probe: ProbeFn | null = null;

  constructor(options: BridgeBackendOptions = {}) {
    const encryptor = safeStorage.isEncryptionAvailable() ? new SafeStorageEncryptor() : new XorEncryptor();
    this.keyVault = new KeyVault(encryptor, options.secretStore ?? new InMemorySecretStore());
    const noopUploader: TelemetryUploader = {
      async upload(): Promise<UploadResult> {
        return { accepted: 0, rejected: 0, reasons: [] };
      },
    };
    this.telemetry = new TelemetrySdk({ uploader: noopUploader });
  }

  /** 注入引擎探测实现（主进程接线；Web/未接线环境保持 null → keysTest 返回 unverified）。 */
  setProbe(probe: ProbeFn | null): void {
    this.probe = probe;
  }

  /** 引擎首行注入用：返回当前激活 Key 的明文（仅内存，写毕即弃）。 */
  getActiveKey(): string | null {
    if (this.activeKeyId === null) return null;
    return this.keyVault.load(this.activeKeyId);
  }

  async handle(channel: string, payload: unknown): Promise<unknown> {
    const body = (payload ?? {}) as Record<string, unknown>;
    switch (channel) {
      case BRIDGE_CHANNELS.authLogin:
        return this.login(body);
      case BRIDGE_CHANNELS.authRegister:
        return this.register(body);
      case BRIDGE_CHANNELS.authLogout:
        this.sessionUsername = null;
        return {};
      case BRIDGE_CHANNELS.keysList:
        return this.keysList();
      case BRIDGE_CHANNELS.keysSave:
        return this.keysSave(body);
      case BRIDGE_CHANNELS.keysTest:
        return this.keysTest(body);
      case BRIDGE_CHANNELS.keysDelete:
        return this.keysDelete(body);
      case BRIDGE_CHANNELS.keysUsage:
        return { modelCalls: 0, models: [], totalTokens: 0, estimatedCostCents: null, ratedCalls: 0 };
      case BRIDGE_CHANNELS.billingOverview:
        return { planName: "免费版", subEndAt: null, pointsBalance: 93 };
      case BRIDGE_CHANNELS.billingLedger:
        return this.billingLedger();
      case BRIDGE_CHANNELS.billingExport:
        return { filename: "erdos-ledger.csv" };
      case BRIDGE_CHANNELS.contentList:
        return DEMO_CONTENT;
      case BRIDGE_CHANNELS.historyList:
        return DEMO_HISTORY;
      case BRIDGE_CHANNELS.historyResume:
        return { taskId: String(body["taskId"] ?? ""), resumable: true };
      case BRIDGE_CHANNELS.complianceExport:
        return this.complianceExport(body);
      case BRIDGE_CHANNELS.entitlementStatus:
        return { status: "ready", balance: 93, graceDeadlineMs: Date.now() + 72 * 3600 * 1000 };
      default:
        throw new Error(`未注册业务通道：${channel}`);
    }
  }

  /** 遥测入口（preload telemetry 通道用，经白名单 + 隐私过滤）。 */
  captureTelemetry(eventName: string, props?: Record<string, unknown>): void {
    this.telemetry.capture({ event_name: eventName, distinct_id: this.sessionUsername ?? "anonymous", props });
  }

  private login(body: Record<string, unknown>): { username: string; expiresInMs: number } {
    const username = String(body["username"] ?? "demo");
    if (username === "demo-error") {
      throw new Error("演示异常态：云端暂时不可用（HTTP 503）");
    }
    this.sessionUsername = username;
    return { username, expiresInMs: 900_000 };
  }

  private register(body: Record<string, unknown>): { username: string; expiresInMs: number } {
    return this.login(body);
  }

  private keysList(): Array<{ id: string; alias: string; baseUrl: string; masked: string; status: string }> {
    return [...this.keyMetas.entries()].map(([id, meta]) => ({
      id,
      alias: meta.alias,
      baseUrl: meta.baseUrl,
      masked: meta.masked,
      status: meta.status,
    }));
  }

  private keysSave(body: Record<string, unknown>): { ok: boolean } {
    const alias = String(body["alias"] ?? "");
    const key = String(body["key"] ?? "");
    const baseUrl = String(body["baseUrl"] ?? "");
    if (!key) return { ok: false };
    const id = `k-${this.keyMetas.size + 1}`;
    this.keyVault.save(id, key);
    this.keyMetas.set(id, { alias, baseUrl, masked: maskSecret(key), status: "unknown" });
    // 最近保存者为激活 Key：向导「保存并测试连通」即测刚保存的 Key（FE-KEYIN 语义）
    this.activeKeyId = id;
    return { ok: true };
  }

  /**
   * Key 连通测试（keysTest 真实化）：
   * - 有引擎探测 → 先 reloadKey（以最新激活 Key 重启注入）再 provider_test；
   * - 无引擎（Web/未接线）→ unverified（已保存未检测，防误判连通）；
   * - 结果回写列表状态（ok/fail/unknown），供 keys 页展示。
   */
  private async keysTest(body: Record<string, unknown>): Promise<KeyTestOutcome> {
    const outcome = await keysTestOutcome({
      key: String(body["key"] ?? ""),
      baseUrl: String(body["baseUrl"] ?? ""),
      model: body["model"] === undefined ? undefined : String(body["model"]),
      probe: this.probe ?? undefined,
    });
    if (this.activeKeyId !== null) {
      const meta = this.keyMetas.get(this.activeKeyId);
      if (meta && outcome.reason !== "invalid") {
        meta.status = outcome.reason === "none" ? "ok" : outcome.reason === "unverified" ? "unknown" : "fail";
      }
    }
    return outcome;
  }

  /**
   * 删除 Key（FE-KEYIN W5）：密文与元数据一并清除。
   * 删除的是当前激活 Key 时置空激活态并标记 requeue，UI 据此引导「任务恢复需重新配置 Key」。
   */
  private keysDelete(body: Record<string, unknown>): { ok: boolean; requeue: boolean } {
    const id = String(body["id"] ?? "");
    if (!id || !this.keyMetas.has(id)) return { ok: false, requeue: false };
    this.keyVault.remove(id);
    this.keyMetas.delete(id);
    const requeue = this.activeKeyId === id;
    if (requeue) this.activeKeyId = null;
    return { ok: true, requeue };
  }

  private billingLedger(): Array<{ ts: string; action: string; stage: string; points: number; taskId: string }> {
    return Array.from({ length: 120 }, (_, i) => ({
      ts: `2026-10-${String((i % 28) + 1).padStart(2, "0")}T09:00:00Z`,
      action: i % 3 === 0 ? "grant" : "consume",
      stage: STAGES[i % 4],
      points: i % 3 === 0 ? 100 : -(1 + (i % 7)),
      taskId: `t-${1000 + (i % 50)}`,
    }));
  }

  private complianceExport(body: Record<string, unknown>): { content: string; filename: string; artifactHashes: string[] } {
    const format = String(body["format"] ?? "md");
    return {
      content: `（演示模式）AI 工具使用声明\n任务：demo-task\n格式：${format}`,
      filename: `AI工具使用声明_demo-task.${format === "docx" ? "docx" : format === "latex" ? "tex" : "md"}`,
      artifactHashes: [],
    };
  }
}
