/** 编译入口真实 IPC 冒烟，验证正式分发文件能启动；只用合成题面和临时 OCR 目录。 */
import { fork } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const require = createRequire(import.meta.url);
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cache = await mkdtemp(join(tmpdir(), "erdos-compiled-import-"));
const execPath = process.argv.includes("--electron") ? require("electron") : process.execPath;
const child = fork(join(root, "dist", "main", "problem-extract-process.js"), [], {
  execPath, env: { ELECTRON_RUN_AS_NODE: "1", PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, TEMP: tmpdir(), TMP: tmpdir() },
  windowsHide: true, serialization: "advanced", stdio: ["ignore", "ignore", "ignore", "ipc"],
});
let timer;
const stopped = new Promise(resolveStop => { child.once("exit", resolveStop); child.once("error", resolveStop); });
try {
  const result = await new Promise((resolveResult, reject) => {
    timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("编译导入入口超时")); }, 10000);
    child.once("error", reject);
    child.once("exit", code => { if (code !== 0) reject(new Error("编译导入入口退出异常")); });
    child.on("message", message => {
      if (message.kind === "result") resolveResult(message.text);
      else if (message.kind === "error") reject(new Error(message.message));
    });
    child.send({ extension: ".txt", bytes: Buffer.from("compiled import fixture"), ocrCachePath: cache });
  });
  assert.equal(result, "compiled import fixture");
  assert.equal(await stopped, 0, "解析进程应正常退出，不能将强制回收计作通过");
  console.log("编译解析入口实际启动与 IPC 通过（" + (process.argv.includes("--electron") ? "Electron Node" : "Node") + "）");
} finally {
  clearTimeout(timer); child.kill("SIGKILL"); await stopped;
  const boundary = relative(resolve(tmpdir()), resolve(cache));
  assert.ok(boundary && !boundary.startsWith(".." + sep));
  await rm(cache, { recursive: true, force: true });
}
