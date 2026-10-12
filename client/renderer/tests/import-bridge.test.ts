// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join, relative, resolve, sep } from "node:path";
import { tmpdir } from "node:os";
vi.mock("electron", () => ({ safeStorage: {} }));
import { BridgeBackend } from "../../main/ipc/bridge.ts";
import { ProblemImporter } from "../../main/problem-import.ts";
import { BRIDGE_CHANNELS as C } from "../../shared/bridge-channels.ts";
const directories: string[] = [];
afterEach(async () => { for (const directory of directories.splice(0)) {
  const rel = relative(resolve(tmpdir()), directory); expect(rel && !rel.startsWith(".." + sep)).toBeTruthy();
  await rm(directory, { recursive: true, force: true });
} });
describe("导入生命周期与真实业务桥接线", () => {
  it("进度与取消按操作号隔离，登出和应用退出回收正在处理的导入", async () => {
    const dir = await mkdtemp(join(tmpdir(), "erdos-import-bridge-")); directories.push(dir);
    const path = join(dir, "题面.txt"); await writeFile(path, "fixture");
    let ready!: () => void; const started = new Promise<void>(yes => { ready = yes; });
    const importer = new ProblemImporter({ select: async () => path, ocrCachePath: dir,
      extract: async () => { ready(); return new Promise(() => {}); } });
    const bridge = new BridgeBackend({ problemImporter: importer, encryptor: { encrypt: v => "fixture:" + v, decrypt: v => v.slice(8) } });
    const pending = bridge.handle(C.taskImport, { importId: "import-bridge" }); await started;
    expect(await bridge.handle(C.taskImportStatus, { importId: "import-bridge" })).toEqual({ importId: "import-bridge", phase: "extracting", progress: 0 });
    expect(await bridge.handle(C.taskImportCancel, { importId: "import-foreign" })).toEqual({ cancelled: false });
    await bridge.handle(C.authLogout, {}); expect(await pending).toBeNull();
    bridge.dispose(); await expect(bridge.handle(C.taskImport, {})).rejects.toThrow("已关闭");
  });
  it("未配置解析器时状态为空、取消幂等，导入失败给出可执行的手动输入提示", async () => {
    const bridge = new BridgeBackend({ encryptor: { encrypt: v => "fixture:" + v, decrypt: v => v.slice(8) } });
    expect(await bridge.handle(C.taskImportStatus, { importId: "import-none" })).toBeNull();
    expect(await bridge.handle(C.taskImportCancel, { importId: "import-none" })).toEqual({ cancelled: false });
    await expect(bridge.handle(C.taskImport, {})).rejects.toThrow("手动粘贴"); bridge.dispose();
  });
});
