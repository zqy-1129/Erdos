/**
 * Web 开发模式桥（SP3-4 演示实现）：无 Electron 时供 vite dev 与浏览器验收。
 *
 * - invoke 各业务通道返回演示数据（登录态写 localStorage 保持刷新不丢）；
 * - 特殊账号演示三态：用户名 demo-empty（空态）／demo-error（异常态，invoke 抛错）；
 * - startDemoTask 注入引擎事件流（默认 10ms/条 ≈ 100 条/s，覆盖四阶段 + 一次门禁失败），
 *   同时服务流式渲染验收与压力场景（可调 intervalMs）。
 * 真实 preload（contextBridge）实现留 SP3-7；本桥数据全部虚拟，不触网（页面不出网红线）。
 */

import type {
  BillingLedgerRow,
  BillingOverview,
  ComplianceExportResult,
  ContentItem,
  EntitlementView,
  ErdosBridge,
  HistoryTask,
  KeyTestResult,
} from "./bridge.ts";
import { BRIDGE_CHANNELS } from "./bridge.ts";
import type { EngineEvent, StageName } from "../../../shared/ipc.ts";
import { exportDeclaration } from "../../../declaration/export.ts";
import type { TrailSource } from "../../../declaration/types.ts";
import { estimateUsage } from "../../../shared/usage.ts";
import type { UsageEstimateView } from "./bridge.ts";

const SESSION_KEY = "erdos.demo.session";
const STAGES: StageName[] = ["analysis", "modeling", "solving", "writing"];

/** 演示留痕（对应 SP1-6 四类事件 + 产物索引；仅供开发模式渲染真实模板）。 */
const DEMO_TRAIL: TrailSource = {
  events: async () => [
    { id: 1, task_id: "demo-task", stage: "analysis", event_type: "model_call", detail: { model: "deepseek-chat", usage: { prompt_tokens: 800, completion_tokens: 1200 }, duration_ms: 800 }, ts: "2026-10-02T08:00:00Z" },
    { id: 2, task_id: "demo-task", stage: "modeling", event_type: "model_call", detail: { model: "deepseek-chat", usage: { prompt_tokens: 300, completion_tokens: 400 }, duration_ms: 3000 }, ts: "2026-10-02T08:05:00Z" },
    { id: 3, task_id: "demo-task", stage: "writing", event_type: "manual_edit", detail: { note: "结论段人工改写" }, ts: "2026-10-02T08:20:00Z" },
    { id: 4, task_id: "demo-task", stage: "solving", event_type: "tool_call", detail: { tool: "plot.fig", path: null }, ts: "2026-10-02T08:15:00Z" },
  ],
  artifacts: async () => [
    { task_id: "demo-task", stage: "writing", kind: "paper", file_path: "out/paper.md", sha256: "9f".repeat(32), size_bytes: 4096 },
  ],
};

const DEMO_CONTENT: ContentItem[] = [
  { id: "t1", kind: "template", title: "CUMCM 官方论文模板", tags: ["格式", "国赛"], referenceOnly: false },
  { id: "t2", kind: "template", title: "MCM 官方格式模板", tags: ["格式", "美赛"], referenceOnly: false },
  { id: "c1", kind: "case", title: "2024 A 题一等奖论文（节选）", tags: ["优化", "评价"], referenceOnly: true },
  { id: "c2", kind: "case", title: "2019 C 题机场出租车思路", tags: ["机理", "预测"], referenceOnly: true },
];

const DEMO_HISTORY: HistoryTask[] = [
  { taskId: "t-1001", title: "示例题：嫦娥三号软着陆", status: "writing", updatedAt: "2026-10-02T18:00:00Z", resumable: true },
  { taskId: "t-1002", title: "2024 C 题打磨", status: "done", updatedAt: "2026-10-01T15:30:00Z", resumable: false },
];

function demoSession(): { username: string } | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as { username: string }) : null;
  } catch {
    return null;
  }
}

function saveSession(username: string | null): void {
  try {
    if (username === null) localStorage.removeItem(SESSION_KEY);
    else localStorage.setItem(SESSION_KEY, JSON.stringify({ username }));
  } catch {
    /* 私有模式等无 localStorage：仅内存态 */
  }
}

/** 演示事件发生器（100 条/s 默认，可调）。 */
export class DemoTaskSimulator {
  private readonly emit: (event: EngineEvent) => void;
  private readonly intervalMs: number;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(emit: (event: EngineEvent) => void, intervalMs = 10) {
    this.emit = emit;
    this.intervalMs = intervalMs;
  }

  start(taskId: string): () => void {
    this.stop();
    let index = 0;
    const total = 210; // 四阶段事件序列总长（约 2.1s 演示完整流程）
    this.timer = setInterval(() => {
      const step = index++;
      if (step >= total) {
        this.stop();
        return;
      }
      const stage = STAGES[Math.min(Math.floor(step / 52), STAGES.length - 1)];
      const ts = new Date().toISOString();
      if (step % 52 === 13 && stage === "modeling") {
        // 演示一次门禁失败（US-004：评审意见展示）
        this.emit({
          trace_id: `demo-${step}`,
          event: "gate.failed",
          task_id: taskId,
          gate: "modeling",
          reason: "假设与数据脱节：目标函数未包含变量约束",
          timestamp: ts,
        });
        return;
      }
      if (step % 26 === 0 && step > 0 && stage === "writing") {
        this.emit({
          trace_id: `demo-${step}`,
          event: "artifact.ready",
          task_id: taskId,
          artifact: step % 52 === 0 ? "data" : "figure",
          sha256: "a".repeat(64),
          timestamp: ts,
        });
        return;
      }
      // 工具调用（solving）：成功/失败各一对，演示时间线（FE-TOOLUI W12）
      if (stage === "solving" && step % 52 === 20) {
        this.emit({
          trace_id: `demo-${step}`,
          event: "tool.call",
          task_id: taskId,
          stage: "solving",
          call_id: `call-${step}`,
          tool: "execute_code",
          args_summary: "python 求解脚本（已脱敏摘要）",
          timestamp: ts,
        });
        return;
      }
      if (stage === "solving" && step % 52 === 25) {
        this.emit({
          trace_id: `demo-${step}`,
          event: "tool.result",
          task_id: taskId,
          call_id: `call-${step - 5}`,
          tool: "execute_code",
          ok: true,
          duration_ms: 240,
          result_ref: "out/result.json",
          timestamp: ts,
        });
        return;
      }
      if (stage === "solving" && step % 52 === 33) {
        this.emit({
          trace_id: `demo-${step}`,
          event: "tool.call",
          task_id: taskId,
          stage: "solving",
          call_id: `call-${step}`,
          tool: "plot_figure",
          args_summary: "结果可视化（已脱敏摘要）",
          timestamp: ts,
        });
        return;
      }
      if (stage === "solving" && step % 52 === 38) {
        this.emit({
          trace_id: `demo-${step}`,
          event: "tool.result",
          task_id: taskId,
          call_id: `call-${step - 5}`,
          tool: "plot_figure",
          ok: false,
          duration_ms: 120,
          error: "沙箱执行失败：依赖缺失",
          timestamp: ts,
        });
        return;
      }
      // 模型 token 流（writing）：允许丢帧，仅展示
      if (stage === "writing" && step % 3 === 0) {
        this.emit({
          trace_id: `demo-${step}`,
          event: "model.delta",
          task_id: taskId,
          delta: `（流式输出片段 ${step}）`,
          timestamp: ts,
        });
        return;
      }
      this.emit({
        trace_id: `demo-${step}`,
        event: "stage.progress",
        task_id: taskId,
        stage,
        progress: ((step % 52) + 1) / 52,
        timestamp: ts,
      });
    }, this.intervalMs);
    return () => this.stop();
  }

  stop(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

export class WebDemoBridge implements ErdosBridge {
  private readonly eventHandlers = new Set<(payload: unknown) => void>();
  private readonly simulator: DemoTaskSimulator;

  constructor() {
    this.simulator = new DemoTaskSimulator((event) => {
      for (const handler of [...this.eventHandlers]) handler(event);
    });
  }

  /** 演示用：工作台一键开始示例题（注入四阶段事件流）。 */
  startDemoTask(taskId: string): void {
    this.simulator.start(taskId);
  }

  invoke<T = unknown>(channel: string, payload?: unknown): Promise<T> {
    const body = (payload ?? {}) as Record<string, unknown>;
    const username = body["username"];
    if (username === "demo-error") {
      return Promise.reject(new Error("演示异常态：云端暂时不可用（HTTP 503）"));
    }
    const empty = username === "demo-empty" || demoSession()?.username === "demo-empty";
    switch (channel) {
      case BRIDGE_CHANNELS.authLogin: {
        saveSession(String(username)); // 演示会话写 localStorage：与真实桥的会话恢复语义对齐
        return Promise.resolve({ username: String(username), expiresInMs: 900_000 } as T);
      }
      case BRIDGE_CHANNELS.authRegister: {
        saveSession(String(username));
        return Promise.resolve({ username: String(username), expiresInMs: 900_000 } as T);
      }
      case BRIDGE_CHANNELS.authLogout:
        saveSession(null);
        return Promise.resolve({} as T);
      case BRIDGE_CHANNELS.authSession: {
        // 演示桥对齐真实桥的会话恢复语义（localStorage 中的演示会话）
        const restored = demoSession();
        return Promise.resolve((restored ? { username: restored.username, expiresInMs: 900_000 } : null) as T);
      }
      case BRIDGE_CHANNELS.keysList:
        return Promise.resolve((empty ? [] : [
          { id: "k1", alias: "DeepSeek", baseUrl: "https://api.deepseek.com/v1", masked: "sk-****7f2a", status: "ok" },
        ]) as T);
      case BRIDGE_CHANNELS.keysSave:
        return Promise.resolve({} as T);
      case BRIDGE_CHANNELS.keysDelete:
        return Promise.resolve({ ok: true, requeue: false } as T);
      case BRIDGE_CHANNELS.keysTest: {
        // dev 演示替身（仅浏览器直跑/UI 验收；preview/production 由 bridge-factory 强制真实桥）：
        // 按关键字模拟分类结果，**不代表真实连通**——真实探测走主进程 provider_test（key-probe.ts）。
        const key = String(body["key"] ?? "");
        const result: KeyTestResult = !key
          ? { ok: false, reason: "invalid", detail: "Key 不能为空" }
          : key.includes("401")
            ? { ok: false, reason: "unauthorized", detail: "鉴权失败（401）：请检查 Key 是否正确" }
            : key.includes("net")
              ? { ok: false, reason: "network", detail: "网络不可达：请检查 Base URL 与网络连接" }
              : key.includes("bal")
                ? { ok: false, reason: "balance", detail: "余额不足：该 Key 账户已欠费" }
                : { ok: true, reason: "none", detail: "连通成功（模型响应 200）" };
        return Promise.resolve(result as T);
      }
      case BRIDGE_CHANNELS.keysUsage: {
        // F-002 用量估算：统计本地留痕的模型调用（演示留痕 2 次 model_call）
        return DEMO_TRAIL.events("demo-task").then((trailEvents) => {
          const estimate = estimateUsage(trailEvents.filter((e) => e.event_type === "model_call"));
          const view: UsageEstimateView = {
            modelCalls: estimate.modelCalls,
            models: estimate.models,
            totalTokens: estimate.totalTokens,
            estimatedCostCents: estimate.estimatedCostCents,
            ratedCalls: estimate.ratedCalls,
          };
          return view as T;
        });
      }
      case BRIDGE_CHANNELS.billingOverview: {
        const view: BillingOverview = empty
          ? { planName: "免费版", subEndAt: null, pointsBalance: 0 }
          : { planName: "年订阅", subEndAt: "2026-11-02T00:00:00Z", pointsBalance: 93 };
        return Promise.resolve(view as T);
      }
      case BRIDGE_CHANNELS.billingLedger: {
        const rows: BillingLedgerRow[] = empty
          ? []
          : Array.from({ length: 120 }, (_, i) => ({
              ts: `2026-10-${String((i % 28) + 1).padStart(2, "0")}T${String(9 + (i % 9)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}:00Z`,
              action: i % 3 === 0 ? "grant" : "consume",
              stage: STAGES[i % 4],
              points: i % 3 === 0 ? 100 : -(1 + (i % 7)),
              taskId: `t-${1000 + (i % 50)}`,
            }));
        return Promise.resolve(rows as T);
      }
      case BRIDGE_CHANNELS.billingExport:
        return Promise.resolve({ filename: "erdos-ledger-demo.csv", savedPath: null, canceled: false } as T);
      case BRIDGE_CHANNELS.contentList:
        return Promise.resolve((empty ? [] : DEMO_CONTENT) as T);
      case BRIDGE_CHANNELS.historyList:
        return Promise.resolve((empty ? [] : DEMO_HISTORY) as T);
      case BRIDGE_CHANNELS.historyResume:
        return Promise.resolve({ taskId: String(body["taskId"]), resumable: true } as T);
      case BRIDGE_CHANNELS.complianceExport: {
        const rawFormat = String(body["format"] ?? "md");
        const format: "md" | "latex" | "docx" =
          rawFormat === "latex" ? "latex" : rawFormat === "docx" ? "docx" : "md";
        const unusedAi = Boolean(body["unusedAi"]);
        const humanNote = String(body["humanNote"] ?? "");
        return (async (): Promise<ComplianceExportResult> => {
          if (format === "docx") {
            // Word 为二进制：演示环境生成 docx（校验数据链路）并以 md 文本预览展示
            await exportDeclaration(DEMO_TRAIL, { taskId: "demo-task", format: "docx", humanNote, unusedAi });
            const md = await exportDeclaration(DEMO_TRAIL, { taskId: "demo-task", format: "md", humanNote, unusedAi });
            return {
              content: `${String(md.content)}\n\n（Word 版本已按同一留痕生成；演示环境等效于下载预览。）`,
              filename: "AI工具使用声明_demo-task.docx",
              // 未使用 AI 声明不引用产物支撑材料
              artifactHashes: unusedAi ? [] : md.data.artifactHashes.map((artifact) => artifact.sha256),
            };
          }
          const exported = await exportDeclaration(DEMO_TRAIL, {
            taskId: "demo-task",
            format,
            humanNote,
            unusedAi,
          });
          return {
            content: String(exported.content),
            filename: exported.filename,
            artifactHashes: unusedAi ? [] : exported.data.artifactHashes.map((artifact) => artifact.sha256),
          };
        })().then((value) => value as T);
      }
      case "engine:start_stage": {
        this.startDemoTask(String(body["task_id"] ?? "demo-task"));
        return Promise.resolve({
          task_id: String(body["task_id"] ?? "demo-task"),
          stage: "analysis",
          status: "running",
        } as T);
      }
      case BRIDGE_CHANNELS.entitlementStatus: {
        const view: EntitlementView = {
          status: "ready",
          balance: 93,
          graceDeadlineMs: Date.now() + 72 * 3600 * 1000,
          stale: false,
        };
        return Promise.resolve(view as T);
      }
      default:
        return Promise.reject(new Error(`未知桥通道：${channel}`));
    }
  }

  subscribe(channel: string, handler: (payload: unknown) => void): () => void {
    if (channel !== BRIDGE_CHANNELS.engineEvent) {
      return () => {};
    }
    this.eventHandlers.add(handler);
    return () => {
      this.eventHandlers.delete(handler);
    };
  }
}