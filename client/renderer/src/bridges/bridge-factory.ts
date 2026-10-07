/**
 * 桥工厂（FE-PRELOAD W4）：按构建渠道选择桥实现。
 *
 * - 真实 preload 桥（window.erdos，由 main/preload.ts 暴露）优先；
 * - dev 模式无 preload 桥时回退 WebDemoBridge（浏览器验收/Vite 直跑）；
 * - preview/alpha/production 强制真实桥（无 window.erdos 直接抛错，对齐 DEC-013 渠道隔离）。
 */
/// <reference types="vite/client" />
import type { ErdosBridge } from "./bridge.ts";
import { WebDemoBridge } from "./web-bridge.ts";

/** preload 桥的全局挂载点类型（与 main/preload.ts 的 exposeInMainWorld 对齐）。 */
declare global {
  interface Window {
    erdos?: ErdosBridge;
  }
}

export function createBridge(): ErdosBridge {
  const electronBridge = window.erdos;
  if (electronBridge) {
    return electronBridge;
  }
  // 仅开发渠道允许回退演示桥；生产构建（无 preload 桥）必须失败而非静默降级
  if (import.meta.env.DEV) {
    return new WebDemoBridge();
  }
  throw new Error("未检测到 preload 桥：生产渠道强制真实桥，请在 Electron 环境运行");
}
