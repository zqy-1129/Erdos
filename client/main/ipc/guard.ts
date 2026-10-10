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
  /** 应用构建页面的绝对 file URL；不配置时拒绝全部本地页面。 */
  rendererUrl?: string;
}

/**
 * 判断页面来源是否可信。
 * - file://：精确匹配配置的自身构建页面（允许页面查询与路由哈希）；
 * - dev 渠道追加 devServerUrl 的完整 origin（协议、域名、端口）；
 * - 其余（http(s) 第三方、chrome-extension、data:、空值）一律拒绝（宁严勿松）。
 */
export function isAllowedSenderUrl(frameUrl: string | null | undefined, policy: SenderPolicy): boolean {
  if (typeof frameUrl !== "string" || frameUrl === "") return false;
  try {
    const actual = new URL(frameUrl);
    if (actual.username || actual.password) return false;
    if (actual.protocol === "file:" && policy.rendererUrl) {
      const expected = new URL(policy.rendererUrl);
      actual.search = ""; actual.hash = ""; expected.search = ""; expected.hash = "";
      return expected.protocol === "file:" && actual.href === expected.href;
    }
    if (policy.dev && ["http:", "https:"].includes(actual.protocol)) {
      const expected = new URL(policy.devServerUrl);
      return ["http:", "https:"].includes(expected.protocol) && !expected.username && !expected.password && actual.origin === expected.origin;
    }
    return false;
  } catch { return false; }
}

/** 外部文档链接仅支持普通网页，拒绝脚本、文件、应用深链和携带凭据的地址。 */
export function isAllowedExternalUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password;
  } catch { return false; }
}
