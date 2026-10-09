/**
 * 渲染层 ↔ 主进程 桥（SP3-4）。
 *
 * 架构红线（执行计划）：
 * - 页面禁止直接调用厂商 API；所有云端/引擎访问经桥由主进程代发；
 * - 渲染层不 import main/ 运行时模块（Node 侧代码），仅共享纯类型；
 * - preload（contextBridge）实现留 SP3-7 壳接入；Web 开发模式用演示实现（web-bridge）。
 */

/** 主进程桥：invoke（请求-响应）+ subscribe（事件订阅，返回退订函数）。 */
export interface ErdosBridge {
  invoke<T = unknown>(channel: string, payload?: unknown): Promise<T>;
  subscribe(channel: string, handler: (payload: unknown) => void): () => void;
}

/** 业务通道白名单（跨端单一来源：shared/bridge-channels.ts，此处 re-export 兼容既有 import）。 */
export { BRIDGE_CHANNELS, type BridgeChannel } from "../../../shared/bridge-channels.ts";

// ---------------------------------------------------------------------------
// 桥视图类型（主进程归一后的简化视图；渲染层不感知云端信封）
// ---------------------------------------------------------------------------

export interface SessionView {
  username: string;
  expiresInMs: number;
}

export interface EntitlementView {
  status: "empty" | "ready" | "frozen" | "grace_expired";
  balance: number;
  /** 宽限到期时刻（绝对 ms 时间戳；null=无快照），供断网横幅倒计时。 */
  graceDeadlineMs: number | null;
  /** true=联网刷新失败、本次为本地快照视图（显示「同步失败」轻提示）。 */
  stale: boolean;
}

export interface KeyItemView {
  id: string;
  alias: string;
  baseUrl: string;
  masked: string;
  /** 主进程侧连通状态缓存：unknown | ok | fail */
  status: "unknown" | "ok" | "fail";
}

export interface KeyTestResult {
  ok: boolean;
  /**
   * 可读失败分类（US-002：401/网络/余额）；
   * unverified = 已保存但未检测（Web/未接线环境，不冒充连通成功）。
   */
  reason: "none" | "unauthorized" | "network" | "balance" | "invalid" | "unverified";
  detail: string;
}

/** 本地用量估算（F-002：来自留痕的 token 统计与可选费用估算）。 */
export interface UsageEstimateView {
  modelCalls: number;
  models: string[];
  totalTokens: number;
  /** 分（人民币）；null = 存在未定价模型，不做误导性总额。 */
  estimatedCostCents: number | null;
  ratedCalls: number;
}

export interface BillingLedgerRow {
  ts: string;
  action: string;
  stage: string;
  points: number;
  taskId: string;
}

export interface BillingOverview {
  planName: string;
  subEndAt: string | null;
  pointsBalance: number;
}

/** 流水导出结果（billing:export）：真实保存路径或演示文件名；取消=用户主动放弃。 */
export interface BillingExportView {
  /** 建议/实际文件名（取消时为建议名）。 */
  filename: string;
  /** 实际保存路径（用户取消或演示模式为 null）。 */
  savedPath: string | null;
  canceled: boolean;
}

export interface ContentItem {
  id: string;
  kind: "template" | "case";
  title: string;
  tags: string[];
  /** 案例合规提示标记（仅作参照）。 */
  referenceOnly: boolean;
}

export interface HistoryTask {
  taskId: string;
  title: string;
  status: string;
  updatedAt: string;
  /** 是否存在可续检查点。 */
  resumable: boolean;
}

export interface ComplianceExportResult {
  /** 下载内容预览（演示模式）。 */
  content: string;
  filename: string;
  /** 产物 sha256（US-007 声明含产物哈希）。 */
  artifactHashes: string[];
}

/** 声明文件保存结果（compliance:save）：真实保存路径或演示文件名；取消=用户主动放弃。 */
export interface ComplianceSaveResult {
  /** 建议/实际文件名（取消时为建议名）。 */
  filename: string;
  /** 实际保存路径（用户取消或演示模式为 null）。 */
  savedPath: string | null;
  canceled: boolean;
}