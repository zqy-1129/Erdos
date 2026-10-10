/**
 * 主进程窗口（FE-HOST W3）：BrowserWindow + 安全基线 + 渲染层加载 + 导航拦截。
 *
 * 安全基线（客户端开发详细方案 §4.1 / 客户端架构 §5）：
 * - contextIsolation: true / sandbox: true / nodeIntegration: false；
 * - 仅自身页面允许导航；外部 HTTP(S) 链接交给系统浏览器，其余协议拒绝。
 */
import { BrowserWindow, shell } from "electron";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { isAllowedSenderUrl, isAllowedExternalUrl } from "./ipc/guard.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** preload 产物（esbuild 编译为 cjs，见 package.json build:main）。 */
const PRELOAD_PATH = path.join(__dirname, "preload.cjs");

export interface WindowOptions {
  /** 开发模式加载 vite dev server（HMR）；生产加载 dist/index.html。 */
  dev: boolean;
  devServerUrl: string;
  /** 渲染层构建产物目录（相对 client/）。 */
  rendererDist: string;
}

export function createMainWindow(options: WindowOptions): BrowserWindow {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    show: false,
    title: "Erdos",
    backgroundColor: "#f5f6f8",
    webPreferences: {
      preload: PRELOAD_PATH,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      // 渲染层零 Node 能力：devtools 仅 dev 模式放开（W4 渠道隔离细化）
      devTools: options.dev,
    },
  });

  // 冷启动采样模式（compat-check，ERDOS_BOOT_PROBE=1）：保持隐藏——仅采集渲染首屏耗时，采样期间不闪窗；
  // 正常启动（未设置该变量）行为不变。
  win.once("ready-to-show", () => {
    if (process.env.ERDOS_BOOT_PROBE !== "1") win.show();
  });

  // 导航拦截：渲染层内禁止任意跳转，外部链接一律交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedExternalUrl(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    const allowed = isAllowedSenderUrl(url, { dev: options.dev, devServerUrl: options.devServerUrl,
      rendererUrl: pathToFileURL(path.join(options.rendererDist, "index.html")).href });
    if (!allowed) {
      event.preventDefault();
      if (isAllowedExternalUrl(url)) void shell.openExternal(url);
    }
  });

  if (options.dev) {
    void win.loadURL(options.devServerUrl);
  } else {
    void win.loadFile(path.join(options.rendererDist, "index.html"));
  }

  return win;
}
