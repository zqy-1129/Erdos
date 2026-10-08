/**
 * 主进程入口（FE-HOST W3）：app 生命周期 + 单实例锁 + 业务/引擎 IPC 注册 + 退出链。
 *
 * 安全基线（客户端开发详细方案 §4.1）：
 * - ipcMain 侧校验 event.senderFrame（生产仅 file://，开发追加 dev server，见 ipc/guard.ts）；
 * - 启动期引擎捆绑产物篡改校验（FE-PKG W18，fail-closed）；
 * - FE-KEYIN：keysTest 经引擎 provider_test 真实探测（reloadKey 注入最新 Key）。
 */
import { app, ipcMain, BrowserWindow, dialog } from "electron";
import path from "node:path";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createMainWindow } from "./window.ts";
import { EngineHost } from "./engine-host/host.ts";
import { runSmoke } from "./smoke.ts";
import { BridgeBackend } from "./ipc/bridge.ts";
import { isAllowedSenderUrl } from "./ipc/guard.ts";
import { createSqliteSecretStore } from "./sqlite-secret-store.ts";
import { createPlatformEncryptor } from "./safe-storage-encryptor.ts";
import { createEncryptedTokenStore } from "./cloud/session-store.ts";
import { createEncryptedEntitlementStateStore } from "./entitlement/state-store.ts";
import { ensureDeviceFingerprint } from "./device-identity.ts";
import { resolveAuthRuntime } from "./ipc/cloud-auth.ts";
import { verifyEngineDir } from "./tamper-check.ts";
import { resolveChannel } from "./update/policy.ts";
import { setupAutoUpdate } from "./update/updater.ts";
import { BRIDGE_CHANNELS } from "../shared/bridge-channels.ts";
import {
  ENGINE_IPC_CHANNELS,
  type ProviderTestResult,
  type RpcMethod,
} from "../shared/ipc.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const clientRoot = path.resolve(__dirname, "../.."); // dist/main → client/

const isDev = process.argv.includes("--dev") || process.env.ERDOS_DEV === "1";
const DEV_SERVER_URL = process.env.ERDOS_DEV_SERVER_URL ?? "http://localhost:5173";

/** IPC 来源策略（生产仅 file://；开发追加 dev server）。 */
function isTrustedSender(frameUrl: string | undefined): boolean {
  return isAllowedSenderUrl(frameUrl, { dev: isDev, devServerUrl: DEV_SERVER_URL });
}

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
  // FE-KEYIN 落库：密钥密文进 SQLite（node:sqlite，零原生依赖）；驱动不可用自动回退内存并告警
  const secretStore = createSqliteSecretStore(path.join(app.getPath("userData"), "erdos.db"), (message) =>
    console.warn(`[client] ${message}`),
  );
  // SP3-5 鉴权接线：ERDOS_API_BASE_URL 决定运行模式（dev 未配置 → 演示回退；生产未配置 → fail-closed）；
  // 设备指纹持久化于 userData（注册赠分防刷，见 device-identity.ts）
  const runtime = resolveAuthRuntime({ apiBaseUrl: process.env.ERDOS_API_BASE_URL, dev: isDev });
  const fingerprint = ensureDeviceFingerprint(path.join(app.getPath("userData"), "device-fingerprint"));
  console.log(
    `[client] 鉴权模式：${runtime.mode === "cloud" ? `cloud（${runtime.baseUrl}）` : runtime.mode}`,
  );
  // SP3-4 本地安全存储：会话令牌加密落盘（safeStorage 密文进 SQLite；驱动不可用时回退内存并告警）
  const sessionStore = secretStore
    ? createEncryptedTokenStore({
        secrets: secretStore,
        encryptor: createPlatformEncryptor((message) => console.warn(`[client] ${message}`)),
      })
    : null;
  if (!sessionStore) {
    console.warn("[client] 会话落盘不可用（SQLite 降级）：重启后需重新登录");
  }
  console.log(`[client] 会话落盘：${sessionStore ? "已启用（加密）" : "未启用（内存）"}`);
  // SP3-4 第二批：权益快照 + 同步元数据加密落盘（断网重启仍可展示快照余额与 72h 宽限倒计时）
  const entitlementStore = secretStore
    ? createEncryptedEntitlementStateStore({
        secrets: secretStore,
        encryptor: createPlatformEncryptor((message) => console.warn(`[client] ${message}`)),
      })
    : null;
  if (!entitlementStore) {
    console.warn("[client] 权益快照落盘不可用（SQLite 降级）：重启后需联网刷新恢复宽限");
  }
  console.log(`[client] 权益快照落盘：${entitlementStore ? "已启用（加密）" : "未启用（内存）"}`);
  bridgeBackend = new BridgeBackend({
    secretStore,
    auth: { runtime, fingerprint, platform: process.platform, sessionStore, entitlementStore },
    // 会话失效下发：业务 401 清会话后推给渲染层（回登录页；见 app-stores.bindSessionInvalidation）
    onSessionInvalidated: () => {
      mainWindow?.webContents.send(BRIDGE_CHANNELS.authSessionInvalidated, { reason: "unauthorized" });
    },
  });
  // 事件型通道（主进程 → 渲染层推送）：不注册 ipcMain.handle
  const eventChannels = new Set<string>([BRIDGE_CHANNELS.engineEvent, BRIDGE_CHANNELS.authSessionInvalidated]);
  for (const channel of Object.values(BRIDGE_CHANNELS)) {
    if (eventChannels.has(channel)) continue;
    ipcMain.handle(channel, (event, payload) => {
      if (!isTrustedSender(event.senderFrame?.url)) {
        return Promise.reject(new Error("非法调用来源：已拒绝"));
      }
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
    ipcMain.handle(channel, (event, params) => {
      if (!isTrustedSender(event.senderFrame?.url)) {
        return Promise.reject(new Error("非法调用来源：已拒绝"));
      }
      if (!engineHost) return Promise.reject(new Error("引擎宿主未初始化"));
      return engineHost.invoke(method, (params ?? {}) as Record<string, unknown>);
    });
  }
}

/**
 * 启动期引擎捆绑产物篡改校验（FE-PKG W18 / SP5-3，fail-closed）：
 * 打包后 resources/engine/ 携带 manifest.json（W17 打包脚本生成）；
 * 任一文件缺失/哈希不一致/清单不可读 → 提示并拒绝启动。
 * 开发期（无捆绑产物）跳过——与 scripts/verify-engine-version.mjs 同口径。
 */
function verifyBundledEngine(): void {
  const bundled = path.join(process.resourcesPath, "engine");
  if (!existsSync(bundled)) return;
  const result = verifyEngineDir(bundled);
  if (!result.ok) {
    dialog.showErrorBox(
      "引擎完整性校验失败",
      `检测到引擎文件被篡改或缺失，已拒绝启动：\n${result.mismatches.join("\n")}`,
    );
    app.exit(1);
  }
}

function boot(): void {
  verifyBundledEngine();
  mainWindow = createMainWindow({
    dev: isDev,
    devServerUrl: DEV_SERVER_URL,
    rendererDist: path.join(clientRoot, "dist"),
  });
  registerBridgeIpc();
  registerEngineIpc();

  // FE-KEYIN/W12：keysTest 真实化——重启引擎注入最新激活 Key，再经 provider_test 探测
  bridgeBackend?.setProbe(async (params) => {
    if (!engineHost) throw new Error("引擎宿主未初始化");
    await engineHost.reloadKey();
    return engineHost.invoke<ProviderTestResult>("provider_test", { ...params });
  });

  // F-102 自动更新：渠道门禁（preview/dev 禁用）+ electron-updater（latest.yml）
  setupAutoUpdate({
    channel: resolveChannel(process.env, isDev),
    onLog: (line) => console.log(line),
  });

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

/**
 * 壳级冒烟模式（ERDOS_SMOKE=1，FE-HOST W3 验收预演 / 10-24 J-1024 前置）：
 * 不建窗口、不走单实例锁，直接跑「引擎启动→握手→四阶段→论文」链路，
 * 退出码 0=通过 / 1=失败，证据 JSON 由 runSmoke 写入 ERDOS_SMOKE_OUT。
 */
if (process.env.ERDOS_SMOKE === "1") {
  const outPath = process.env.ERDOS_SMOKE_OUT ?? path.join(clientRoot, "dist", "smoke-evidence.json");
  const home = process.env.ERDOS_SMOKE_HOME ?? path.join(app.getPath("temp"), `erdos-smoke-${Date.now()}`);
  void app.whenReady().then(async () => {
    const { command, args, cwd } = engineCommand();
    const evidence = await runSmoke({
      command,
      args,
      cwd,
      home,
      outPath,
      key: process.env.ERDOS_SMOKE_KEY ?? null,
      // FakeLLM 下 solving 走真实沙箱执行（实测 ~60s+），默认放宽到 180s，可用环境变量覆盖
      stageTimeoutMs: Number(process.env.ERDOS_SMOKE_STAGE_TIMEOUT_MS ?? "180000"),
      onLog: (line) => console.log(line),
    });
    console.log(`[smoke] ${evidence.ok ? "通过" : "失败"}：证据 → ${outPath}`);
    app.exit(evidence.ok ? 0 : 1);
  });
} else {
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
}
