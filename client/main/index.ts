/**
 * 主进程入口（FE-HOST W3）：app 生命周期 + 单实例锁 + 业务/引擎 IPC 注册 + 退出链。
 */
import { app, ipcMain, BrowserWindow } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createMainWindow } from "./window.ts";
import { EngineHost } from "./engine-host/host.ts";
import { BridgeBackend } from "./ipc/bridge.ts";
import { BRIDGE_CHANNELS } from "../shared/bridge-channels.ts";
import { ENGINE_IPC_CHANNELS, type RpcMethod } from "../shared/ipc.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const clientRoot = path.resolve(__dirname, "../.."); // dist/main → client/

const isDev = process.argv.includes("--dev") || process.env.ERDOS_DEV === "1";
const DEV_SERVER_URL = process.env.ERDOS_DEV_SERVER_URL ?? "http://localhost:5173";

/** 引擎可执行：开发期用 engine/.venv 的 python；可用 ERDOS_ENGINE_CMD 覆盖（打包期指向 PyInstaller 产物）。 */
function engineCommand(): { command: string; args: string[]; cwd: string } {
  // `python -m engine` 需能定位 engine 包 → cwd 指向其父目录（Edros 仓库根）
  const engineRoot = path.join(clientRoot, "..");
  if (process.env.ERDOS_ENGINE_CMD) {
    return { command: process.env.ERDOS_ENGINE_CMD, args: ["-m", "engine"], cwd: engineRoot };
  }
  const python = path.join(clientRoot, "..", "engine", ".venv", "Scripts", "python.exe");
  return { command: python, args: ["-m", "engine"], cwd: engineRoot };
}

let mainWindow: BrowserWindow | null = null;
let engineHost: EngineHost | null = null;
let bridgeBackend: BridgeBackend | null = null;

/** 引擎通道 → RPC 方法名（engine:event 与业务通道不在此列）。 */
function channelToMethod(channel: string): RpcMethod | null {
  if (channel === "engine:event" || !channel.startsWith("engine:")) return null;
  return channel.slice("engine:".length) as RpcMethod;
}

function registerBridgeIpc(): void {
  bridgeBackend = new BridgeBackend();
  for (const channel of Object.values(BRIDGE_CHANNELS)) {
    if (channel === BRIDGE_CHANNELS.engineEvent) continue;
    ipcMain.handle(channel, (_event, payload) => {
      if (!bridgeBackend) return Promise.reject(new Error("桥后端未初始化"));
      return bridgeBackend.handle(channel, payload);
    });
  }
}

function registerEngineIpc(): void {
  const home = path.join(app.getPath("userData"), "engine-home");
  const { command, args, cwd } = engineCommand();

  engineHost = new EngineHost({
    command,
    args,
    cwd,
    home,
    // W5：spawn 时从 key-vault 取激活 Key 明文注入（无则空行 → 无 Key 模式，FakeLLM）
    key: () => bridgeBackend?.getActiveKey() ?? null,
    onEvent: (event) => {
      mainWindow?.webContents.send("engine:event", event);
    },
    onStateChange: (state) => {
      console.log(`[engine-host] state → ${state}`);
    },
    onLog: (line) => {
      console.log(`[engine] ${line}`);
    },
    onProtocolError: (err) => {
      console.warn(`[engine-host] 协议错误：${err.message}`);
    },
  });

  for (const channel of ENGINE_IPC_CHANNELS) {
    const method = channelToMethod(channel);
    if (method === null) continue;
    ipcMain.handle(channel, (_event, params) => {
      if (!engineHost) return Promise.reject(new Error("引擎宿主未初始化"));
      return engineHost.invoke(method, (params ?? {}) as Record<string, unknown>);
    });
  }
}

function boot(): void {
  mainWindow = createMainWindow({
    dev: isDev,
    devServerUrl: DEV_SERVER_URL,
    rendererDist: path.join(clientRoot, "dist"),
  });
  registerBridgeIpc();
  registerEngineIpc();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createMainWindow({
        dev: isDev,
        devServerUrl: DEV_SERVER_URL,
        rendererDist: path.join(clientRoot, "dist"),
      });
    }
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
  void app.whenReady().then(boot);
  app.on("before-quit", () => {
    engineHost?.stop();
  });
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
