import assert from "node:assert/strict";
import { after, describe, it } from "node:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve, sep } from "node:path";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { resolveEngineLaunch } from "../main/engine-launch.ts";
import { parseManifest, sha256, verifyEngineDir } from "../main/tamper-check.ts";

const root = mkdtempSync(join(tmpdir(), "erdos-launch-tests-"));
after(() => {
  const rel = relative(resolve(tmpdir()), resolve(root));
  assert.ok(rel && rel !== ".." && !rel.startsWith(".." + sep));
  rmSync(root, { recursive: true, force: true });
});
function bundle(files: Record<string, string> = { "engine.exe": "fixture", "version.txt": "0.1.0" }, extra = {}) {
  const resourcesPath = mkdtempSync(join(root, "bundle-")); const dir = join(resourcesPath, "engine"); mkdirSync(dir);
  for (const [name, content] of Object.entries(files)) { mkdirSync(dirname(join(dir, name)), { recursive: true }); writeFileSync(join(dir, name), content); }
  const manifest = { version: 1, engineVersion: "0.1.0", files: Object.fromEntries(Object.entries(files).map(([name, content]) => [name, sha256(Buffer.from(content))])), ...extra };
  writeFileSync(join(dir, "manifest.json"), JSON.stringify(manifest));
  return { dir, manifest, options: { packaged: true, clientRoot: root, resourcesPath, platform: "win32" as const, expectedVersion: "0.1.0" } };
}
describe("W18 捆绑引擎启动与严格打包门禁", () => {
  it("Windows/Linux开发解释器与覆盖命令保持Python模块参数", () => {
    for (const platform of ["win32", "linux"] as const) {
      const options = { packaged: false, clientRoot: join(root, "client"), resourcesPath: root, platform };
      const launch = resolveEngineLaunch(options); assert.deepEqual(launch.args, ["-m", "engine"]);
      assert.match(launch.command, platform === "win32" ? /python\.exe$/ : /bin[\\/]python$/);
      assert.equal(resolveEngineLaunch({ ...options, developmentCommand: "fixture-python" }).command, "fixture-python");
    }
  });
  it("正式包执行已校验入口，忽略开发覆盖命令且不追加-m engine", () => {
    const b = bundle(); const launch = resolveEngineLaunch({ ...b.options, developmentCommand: "evil.exe" });
    assert.equal(launch.command, join(b.dir, "engine.exe")); assert.equal(launch.cwd, b.dir); assert.deepEqual(launch.args, []);
  });
  it("多个入口需显式选择；Linux和嵌套入口均由清单确定", () => {
    const ambiguous = bundle({ "a.exe": "a", "b.exe": "b", "version.txt": "0.1.0" });
    assert.throws(() => resolveEngineLaunch(ambiguous.options), /入口不明确/);
    const selected = bundle({ "nested/engine.exe": "a", "other.exe": "b", "version.txt": "0.1.0" }, { entrypoint: "nested/engine.exe" });
    assert.equal(resolveEngineLaunch(selected.options).cwd, join(selected.dir, "nested"));
    const linux = bundle({ "engine": "linux", "version.txt": "0.1.0" });
    assert.equal(resolveEngineLaunch({ ...linux.options, platform: "linux" }).command, join(linux.dir, "engine"));
  });
  it("缺资源、缺版本哈希、版本漂移、无构建绑定或入口异常均拒绝启动", () => {
    const b = bundle(); assert.throws(() => resolveEngineLaunch({ ...b.options, resourcesPath: join(root, "missing") }), /缺少捆绑/);
    assert.throws(() => resolveEngineLaunch({ ...b.options, expectedVersion: undefined }), /版本/);
    assert.throws(() => resolveEngineLaunch({ ...b.options, expectedVersion: "0.2.0" }), /版本/);
    const bad = bundle({ "engine.exe": "fixture", "version.txt": "0.2.0" }); assert.throws(() => resolveEngineLaunch(bad.options), /版本/);
    const unlisted = bundle({ "engine.exe": "fixture" }); writeFileSync(join(unlisted.dir, "version.txt"), "0.1.0");
    assert.throws(() => resolveEngineLaunch(unlisted.options), /版本/);
    const unsupported = bundle({ "engine": "fixture", "version.txt": "0.1.0" }, { entrypoint: "engine" });
    assert.throws(() => resolveEngineLaunch(unsupported.options), /入口/);
    writeFileSync(join(b.dir, "engine.exe"), "changed"); assert.throws(() => resolveEngineLaunch(b.options), /完整性/);
  });
  it("清单拒绝空值、越界、Windows路径别名、版本和哈希损坏", () => {
    const b = bundle(); const badFiles = [{}, [], { "../evil.exe": sha256(Buffer.from("x")) }, { "C:/evil.exe": sha256(Buffer.from("x")) },
      { "engine.exe:stream": sha256(Buffer.from("x")) }, { "nested\\engine.exe": sha256(Buffer.from("x")) }, { "file.": sha256(Buffer.from("x")) },
      { "a//b": sha256(Buffer.from("x")) }, { "a/./b": sha256(Buffer.from("x")) }, { "a.exe": "bad hash" }];
    for (const files of badFiles) assert.throws(() => parseManifest(JSON.stringify({ ...b.manifest, files })));
    for (const extra of [{ version: 2 }, { engineVersion: "" }, { entrypoint: "unlisted.exe" }]) assert.throws(() => parseManifest(JSON.stringify({ ...b.manifest, ...extra })));
    assert.throws(() => parseManifest(" ".repeat(5 * 1024 * 1024 + 1)), /过大/);
  });
  it("资源目录符号链接、清单文件指向目录和不可读目录均拒绝，额外资源不影响校验", () => {
    const b = bundle(); mkdirSync(join(b.dir, "extra")); symlinkSync(root, join(b.dir, "extra", "loop"), "junction");
    assert.equal(verifyEngineDir(b.dir).ok, true);
    rmSync(join(b.dir, "engine.exe")); mkdirSync(join(b.dir, "engine.exe")); assert.equal(verifyEngineDir(b.dir).ok, false);
    const linked = bundle({ "internal/library.dll": "dll", "engine.exe": "fixture", "version.txt": "0.1.0" });
    const target = join(root, "external"); mkdirSync(target); writeFileSync(join(target, "library.dll"), "dll");
    rmSync(join(linked.dir, "internal"), { recursive: true }); symlinkSync(target, join(linked.dir, "internal"), "junction");
    assert.equal(verifyEngineDir(linked.dir).ok, false);
  });
  it("版本预检开发期可明确跳过，pack严格模式缺资源失败；有效/漂移资源按同一启动规则验证", () => {
    const script = fileURLToPath(new URL("../scripts/verify-engine-version.mjs", import.meta.url));
    const missing = join(root, "absent");
    assert.match(execFileSync(process.execPath, [script, "--resources-dir", missing], { encoding: "utf8" }), /跳过/);
    assert.equal(spawnSync(process.execPath, [script, "--resources-dir", missing, "--require-resources"], { encoding: "utf8" }).status, 2);
    const b = bundle(); assert.match(execFileSync(process.execPath, [script, "--resources-dir", b.options.resourcesPath, "--require-resources"], { encoding: "utf8" }), /通过/);
    writeFileSync(join(b.dir, "version.txt"), "0.2.0");
    assert.equal(spawnSync(process.execPath, [script, "--resources-dir", b.options.resourcesPath], { encoding: "utf8" }).status, 1);
  });
});
