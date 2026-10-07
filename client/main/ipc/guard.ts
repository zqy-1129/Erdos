/**
 * IPC 调用来源校验（FE-HOST W3 安全基线）：ipcMain 侧校验 event.senderFrame。
 *
 * 红线（《客户端开发详细方案》§4.1）：
 * - ipcMain 侧校验 event.senderFrame 与通道白名单，非法来源直接拒绝并计为异常；
 * - 生产渠道：仅接受应用自身页面（file:// 加载的渲染层产物）；
 * - 开发渠道：额外允许 vite dev server 来源（浏览器直跑 / HMR）。
 *
 * 实现为纯函数（不依赖 electron 运行时），便于 node:test 单测；
 * 宿主接线在 main/index.ts 的 registerBridgeIpc / registerEngineIpc。
 */

export interface SenderPolicy {
  /** 是否开发渠道（--dev / ERDOS_DEV=1）。 */
  dev: boolean;
  /** 开发渠道渲染层来源（vite dev server，如 http://localhost:5173）。 */
  devServerUrl: string;
}

/**
 * 判断页面来源是否可信。
 * - file://：应用自身加载的构建产物（生产与开发均合法）；
 * - dev 渠道追加 devServerUrl 前缀（http://localhost:5173/…）；
 * - 其余（http(s) 第三方、chrome-extension、data:、空值）一律拒绝（宁严勿松）。
 */
export function isAllowedSenderUrl(frameUrl: string | null | undefined, policy: SenderPolicy): boolean {
  if (typeof frameUrl !== "string" || frameUrl === "") return false;
  if (frameUrl.startsWith("file://")) return true;
  if (policy.dev && frameUrl.startsWith(policy.devServerUrl)) return true;
  return false;
}