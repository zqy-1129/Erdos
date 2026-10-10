// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { sha256 } from "../../main/tamper-check.ts";
const mocks = vi.hoisted(() => {
  const handlers = new Map<string, (...args: any[]) => unknown>();
  const events = new Map<string, (...args: any[]) => unknown>();
  const updateEvents = new Map<string, (...args: any[]) => unknown>();
  const store = new Map<string, string>();
  const window = { webContents: { send: vi.fn() }, isMinimized: () => false, restore: vi.fn(), focus: vi.fn() };
  return { handlers, events, updateEvents, window, encryptionAvailable: true, showErrorBox: vi.fn(), createWindow: vi.fn(() => window), engineOptions: [] as any[],
    store: { get: (k: string) => store.get(k) ?? null, set: (k: string, v: string) => store.set(k,v), has: (k: string) => store.has(k) },
    app: { getPath: () => "C:/erdos-unit-fixture", setPath: vi.fn(), getVersion: () => "0.1.0", isPackaged: false,
      requestSingleInstanceLock: () => true, whenReady: () => Promise.resolve(),
      on: (e: string, h: (...args: any[]) => unknown) => events.set(e,h), quit: vi.fn(), exit: vi.fn() },
    updater: { autoDownload: true, autoInstallOnAppQuit: true, setFeedURL: vi.fn(), checkForUpdates: vi.fn(async () => null),
      downloadUpdate: vi.fn(async () => []), quitAndInstall: vi.fn(), on: (e: string, h: (...args: any[]) => unknown) => updateEvents.set(e,h) },
    contextBridge: { exposeInMainWorld: vi.fn() }, ipcRenderer: { invoke: vi.fn(async () => "ok"), on: vi.fn(), removeListener: vi.fn() } };
});
vi.mock("electron", () => ({ app: mocks.app, BrowserWindow: { getAllWindows: () => [mocks.window] },
  ipcMain: { handle: (c: string, h: (...args: any[]) => unknown) => mocks.handlers.set(c,h) },
  dialog: { showOpenDialog: vi.fn(async () => ({ canceled: true })), showErrorBox: mocks.showErrorBox },
  contextBridge: mocks.contextBridge, ipcRenderer: mocks.ipcRenderer,
  safeStorage: { isEncryptionAvailable: () => mocks.encryptionAvailable, encryptString: (v: string) => Buffer.from(v), decryptString: (v: Buffer) => v.toString() } }));
vi.mock("electron-updater", () => ({ autoUpdater: mocks.updater }));
vi.mock("../../main/window.ts", () => ({ createMainWindow: mocks.createWindow }));
vi.mock("../../main/sqlite-secret-store.ts", () => ({ createSqliteSecretStore: () => mocks.store }));
vi.mock("../../main/device-identity.ts", () => ({ ensureDeviceFingerprint: () => "unit-fingerprint" }));
vi.mock("../../main/save-export.ts", () => ({ createExportSaver: () => async () => ({ canceled: true, path: null }), createFileSaver: () => async () => ({ canceled: true, path: null }) }));
vi.mock("../../main/engine-host/host.ts", () => ({ EngineHost: class {
  constructor(options: unknown) { mocks.engineOptions.push(options); }
  currentState = "idle"; stop = vi.fn(); reloadKey = vi.fn(async () => {});
  invoke = vi.fn(async () => ({ engine: "idle", task: null }));
} }));
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); mocks.encryptionAvailable = true; mocks.app.isPackaged = false; });
describe("Electron 入口与预加载实际接线", () => {
  it("入口注册全部业务/引擎通道并拒绝非法来源", async () => {
    vi.stubEnv("ERDOS_API_BASE_URL", "");
    vi.stubGlobal("process", { ...process, resourcesPath: "C:/erdos-unit-resources" });
    await import("../../main/index.ts"); await Promise.resolve(); await Promise.resolve();
    expect(mocks.handlers.has("keys:save")).toBe(true); expect(mocks.handlers.has("engine:start_stage")).toBe(true);
    expect(mocks.handlers.has("engine:event")).toBe(false);
    const valid = { senderFrame: { url: pathToFileURL(resolve("dist/index.html")).href } };
    await expect(mocks.handlers.get("keys:list")!(valid, {})).resolves.toEqual([]);
    await expect(mocks.handlers.get("keys:list")!({ senderFrame: { url: "https://evil.example" } }, {})).rejects.toThrow("非法");
    await expect(mocks.handlers.get("engine:get_status")!({ senderFrame: { url: "https://evil.example" } }, {})).rejects.toThrow("非法");
    await expect(mocks.handlers.get("engine:get_status")!(valid, {})).rejects.toThrow("云端");
    mocks.events.get("second-instance")!(); expect(mocks.window.focus).toHaveBeenCalled();
    mocks.events.get("activate")!(); mocks.events.get("before-quit")!(); mocks.events.get("window-all-closed")!();
  });
  it("预加载通道白名单在运行时生效并可取消订阅", async () => {
    await import("../../main/preload.ts");
    const bridge = mocks.contextBridge.exposeInMainWorld.mock.calls.at(-1)![1] as { invoke: (c: string) => Promise<unknown>; subscribe: (c: string, handler: () => void) => () => void };
    await expect(bridge.invoke("keys:list")).resolves.toBe("ok");
    expect(() => bridge.invoke("node:readFile")).toThrow("未授权");
    expect(() => bridge.subscribe("keys:list", () => {})).toThrow("未授权");
    const handler = vi.fn(); const dispose = bridge.subscribe("engine:event", handler);
    const listener = mocks.ipcRenderer.on.mock.calls.at(-1)![1] as (...args: unknown[]) => void;
    listener({}, { event: "stage.progress" }); expect(handler).toHaveBeenCalled();
    dispose(); expect(mocks.ipcRenderer.removeListener).toHaveBeenCalled();
  });
  it("启动安全存储失败展示通用错误并退出，异常信息不会外显", async () => {
    vi.resetModules(); mocks.encryptionAvailable = false;
    vi.stubEnv("ERDOS_API_BASE_URL", "");
    vi.stubGlobal("process", { ...process, resourcesPath: "C:/erdos-unit-resources" });
    await import("../../main/index.ts"); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(mocks.showErrorBox).toHaveBeenCalledWith("客户端初始化失败", expect.stringContaining("本地安全存储"));
    expect(mocks.app.exit).toHaveBeenCalledWith(1);
  });
  it("正式包缺引擎时在窗口创建前拒绝，不能用dev或smoke环境绕过", async () => {
    vi.resetModules(); mocks.app.isPackaged = true; mocks.createWindow.mockClear();
    vi.stubEnv("ERDOS_DEV", "1"); vi.stubEnv("ERDOS_SMOKE", "1");
    vi.stubGlobal("process", { ...process, resourcesPath: "C:/erdos-unit-missing", argv: ["fixture", "--dev"] });
    await import("../../main/index.ts"); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(mocks.createWindow).not.toHaveBeenCalled();
    expect(mocks.showErrorBox).toHaveBeenCalledWith("引擎启动校验失败", expect.stringContaining("缺少捆绑引擎"));
  });
  it("正式入口接线使用哈希与版本绑定后的exe，关闭开发模式且不传Python参数", async () => {
    const resources = mkdtempSync(resolve(tmpdir(), "erdos-packaged-wiring-"));
    try {
      const engine = resolve(resources, "engine"); mkdirSync(engine);
      writeFileSync(resolve(engine, "engine.exe"), "fixture"); writeFileSync(resolve(engine, "version.txt"), "0.1.0");
      writeFileSync(resolve(engine, "manifest.json"), JSON.stringify({ version: 1, engineVersion: "0.1.0", files: {
        "engine.exe": sha256(Buffer.from("fixture")), "version.txt": sha256(Buffer.from("0.1.0")),
      } }));
      vi.resetModules(); mocks.app.isPackaged = true; mocks.createWindow.mockClear();
      vi.stubEnv("ERDOS_DEV", "1"); vi.stubEnv("ERDOS_ENGINE_CMD", "fixture-evil.exe"); vi.stubEnv("ERDOS_API_BASE_URL", "");
      vi.stubGlobal("__ERDOS_ENGINE_VERSION__", "0.1.0"); vi.stubGlobal("process", { ...process, platform: "win32", resourcesPath: resources });
      await import("../../main/index.ts"); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
      expect(mocks.createWindow).toHaveBeenCalledWith(expect.objectContaining({ dev: false }));
      expect(mocks.engineOptions.at(-1)).toMatchObject({ command: resolve(engine, "engine.exe"), args: [], cwd: engine });
    } finally {
      expect(resources.startsWith(resolve(tmpdir()) + (process.platform === "win32" ? "\\" : "/"))).toBe(true);
      rmSync(resources, { recursive: true, force: true });
    }
  });
  it("平台加密不可用禁止弱加密回退；正式更新源配置与渠道门禁", async () => {
    const { createPlatformEncryptor } = await import("../../main/safe-storage-encryptor.ts");
    mocks.encryptionAvailable = false; const warn = vi.fn(); expect(() => createPlatformEncryptor(warn)).toThrow("平台安全");
    expect(warn).toHaveBeenCalled();
    const { setupAutoUpdate } = await import("../../main/update/updater.ts");
    const base = { version: "0.1.0", packaged: true, onLog: vi.fn(), canInstall: async () => true };
    for (const feedUrl of [undefined, "http://unit.example", "https://user:secret@unit.example"]) {
      expect(setupAutoUpdate({ ...base, channel: "production", feedUrl }).view().status).toBe("disabled");
    }
    const c = setupAutoUpdate({ ...base, channel: "production", feedUrl: "https://updates.example", checkOnStart: false });
    await c.run("check"); mocks.updateEvents.get("update-available")!({ version: "0.2.0" }); await c.run("download");
    mocks.updateEvents.get("update-downloaded")!(); await c.run("install");
    expect(mocks.updater.autoDownload).toBe(false); expect(mocks.updater.autoInstallOnAppQuit).toBe(false);
    expect(mocks.updater.quitAndInstall).toHaveBeenCalledWith(false, true);
  });
});
