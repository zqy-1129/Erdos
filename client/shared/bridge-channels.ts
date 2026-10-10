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
  /** 会话恢复查询（启动时读持久化会话；返回 SessionView | null）。 */
  authSession: "auth:session",
  authResetRequest: "auth:reset_request",
  authResetConfirm: "auth:reset_confirm",
  /** 会话失效下发（主进程业务 401/403 清会话后推送；渲染层回登录页，非 handle 通道）。 */
  authSessionInvalidated: "auth:session-invalidated",
  // Key 管家
  keysList: "keys:list",
  keysSave: "keys:save",
  keysTest: "keys:test",
  keysUsage: "keys:usage",
  keysDelete: "keys:delete",
  keysActivate: "keys:activate",
  preferencesGet: "preferences:get",
  preferencesSave: "preferences:save",
  taskImport: "task:import",
  taskPending: "task:pending",
  taskStatus: "task:status",
  artifactsList: "artifacts:list",
  artifactsPreview: "artifacts:preview",
  artifactsSave: "artifacts:save",
  // 账单
  billingOverview: "billing:overview",
  billingLedger: "billing:ledger",
  billingExport: "billing:export",
  billingProducts: "billing:products",
  billingCreateOrder: "billing:create_order",
  billingOrder: "billing:order",
  billingPendingOrder: "billing:pending_order",
  updateStatus: "update:status",
  updateCheck: "update:check",
  updateDownload: "update:download",
  updateInstall: "update:install",
  // 内容库
  contentList: "content:list",
  // 历史任务
  historyList: "history:list",
  historyResume: "history:resume",
  // 合规导出
  complianceExport: "compliance:export",
  /** 声明文件保存（md/latex 文本 + docx 真实二进制，经原生保存对话框落盘）。 */
  complianceSave: "compliance:save",
  /** 最近任务（本地留痕汇总；合规声明任务来源——重启后可对历史任务出声明）。 */
  trailRecentTasks: "trail:recent_tasks",
  // 权益（断网态 UI 数据源）
  entitlementStatus: "entitlement:status",
  // 遥测（白名单 + 脱敏前置）
  telemetryTrack: "telemetry:track",
  // 引擎事件转发（对齐 shared/ipc.ts 白名单）
  engineEvent: "engine:event",
} as const;

/** 桥通道联合类型。 */
export type BridgeChannel = (typeof BRIDGE_CHANNELS)[keyof typeof BRIDGE_CHANNELS];
