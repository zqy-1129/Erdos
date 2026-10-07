/**
 * 业务桥通道白名单（跨端单一来源）。
 *
 * 渲染层与主进程共享同一份通道常量，避免两端字面量漂移；
 * 主进程 registerBridgeIpc 据此注册 ipcMain.handle，preload 仅透传。
 */
export const BRIDGE_CHANNELS = {
  // 认证
  authLogin: "auth:login",
  authRegister: "auth:register",
  authLogout: "auth:logout",
  // Key 管家
  keysList: "keys:list",
  keysSave: "keys:save",
  keysTest: "keys:test",
  keysUsage: "keys:usage",
  keysDelete: "keys:delete",
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
  // 遥测（白名单 + 脱敏前置）
  telemetryTrack: "telemetry:track",
  // 引擎事件转发（对齐 shared/ipc.ts 白名单）
  engineEvent: "engine:event",
} as const;

/** 桥通道联合类型。 */
export type BridgeChannel = (typeof BRIDGE_CHANNELS)[keyof typeof BRIDGE_CHANNELS];
