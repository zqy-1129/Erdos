// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
const mocks = vi.hoisted(() => {
  const events = new Map<string, (...args: any[]) => unknown>();
  const window = { once: vi.fn(), show: vi.fn(), loadFile: vi.fn(), loadURL: vi.fn(), webContents: {
    setWindowOpenHandler: vi.fn(), on: (event: string, fn: (...args: any[]) => unknown) => events.set(event, fn),
  } };
  return { events, window, openExternal: vi.fn() };
});
vi.mock("electron", () => ({ BrowserWindow: class { constructor() { return mocks.window; } }, shell: { openExternal: mocks.openExternal } }));
import { createMainWindow } from "../../main/window.ts";
afterEach(() => { vi.clearAllMocks(); });
describe("窗口导航与外链实际接线", () => {
  it("只允许应用页面导航，危险协议不传给操作系统", () => {
    const rendererDist = resolve("dist"); createMainWindow({ dev: false, devServerUrl: "http://localhost:5173", rendererDist });
    const navigate = mocks.events.get("will-navigate")!; const preventDefault = vi.fn();
    navigate({ preventDefault }, pathToFileURL(resolve(rendererDist, "index.html")).href + "#/keys");
    expect(preventDefault).not.toHaveBeenCalled();
    for (const url of ["file:///C:/evil.html", "javascript:alert(1)", "erdos://run"]) navigate({ preventDefault }, url);
    expect(preventDefault).toHaveBeenCalledTimes(3); expect(mocks.openExternal).not.toHaveBeenCalled();
    navigate({ preventDefault }, "https://docs.example/help"); expect(mocks.openExternal).toHaveBeenCalledWith("https://docs.example/help");
    const open = mocks.window.webContents.setWindowOpenHandler.mock.calls.at(-1)![0] as (value: { url: string }) => unknown;
    expect(open({ url: "file:///C:/evil.exe" })).toEqual({ action: "deny" });
    expect(open({ url: "https://docs.example/guide" })).toEqual({ action: "deny" });
    expect(mocks.openExternal).toHaveBeenCalledTimes(2);
  });
  it("开发页面严格匹配origin，不接受端口/域名伪前缀", () => {
    createMainWindow({ dev: true, devServerUrl: "http://localhost:5173", rendererDist: resolve("dist") });
    const navigate = mocks.events.get("will-navigate")!; const preventDefault = vi.fn();
    navigate({ preventDefault }, "http://localhost:5173/keys"); expect(preventDefault).not.toHaveBeenCalled();
    navigate({ preventDefault }, "http://localhost:51730/keys"); expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(mocks.window.loadURL).toHaveBeenCalledWith("http://localhost:5173");
  });
});
