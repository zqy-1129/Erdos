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

/** 业务通道白名单（对齐 SP3-1 引擎白名单 + SP3-4 页面所需业务通道）。 */
export const BRIDGE_CHANNELS = {
  // 认证
  authLogin: "auth:login",
  authRegister: "auth:register",
  authLogout: "auth:logout",
  // Key 管家
  keysList: "keys:list",
  keysSave: "keys:save",
  keysTest: "keys:test",
  // 账单
  billingOverview: "billing:overview",
  billingLedger: "billing:ledger",
  billingExport: "billing:export",
  // 内容库
  contentList: "content:list",
  // 历史任务
  historyList: "history:list",
  historyResume: "history:resume",
  // 合规导出
  complianceExport: "compliance:export",
  // 权益（断网态 UI 数据源）
  entitlementStatus: "entitlement:status",
  // 引擎事件转发（对齐 shared/ipc.ts 白名单）
  engineEvent: "engine:event",
} as const;

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
  /** 可读失败分类（US-002：401/网络/余额）。 */
  reason: "none" | "unauthorized" | "network" | "balance" | "invalid";
  detail: string;
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