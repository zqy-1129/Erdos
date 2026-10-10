import { spawn, execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// 独立端口和临时数据库：不访问现有服务或用户数据，不使用厂商 API 凭据。
const clientRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(clientRoot, "..");
const python = process.env.ERDOS_TEST_SERVER_PYTHON ?? [
  join(repoRoot, "server", ".venv", "Scripts", "python.exe"),
  join(repoRoot, "server", ".venv", "bin", "python"),
].find(existsSync);
if (!python) throw new Error("请先安装 server/.venv 依赖，或设置 ERDOS_TEST_SERVER_PYTHON。");
const evidence = await mkdtemp(join(tmpdir(), "erdos-client-integration-"));
const server = spawn(python, [join(clientRoot, "scripts", "integration-server.py"), repoRoot, evidence], {
  stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
  env: Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("ERDOS_") && !key.startsWith("LLM_"))),
});
let output = ""; let diagnostics = ""; let spawnError = null;
server.stdout.on("data", bytes => { output = (output + bytes.toString()).slice(-16000); });
server.stderr.on("data", bytes => { diagnostics = (diagnostics + bytes.toString()).slice(-16000); });
server.on("error", error => { spawnError = error; });
async function ready() {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (spawnError || server.exitCode !== null) throw new Error("独立测试服务启动失败：" + (spawnError?.message ?? diagnostics));
    const baseUrl = output.match(new RegExp("ERDOS_TEST_SERVER_URL=(http://127[.]0[.]0[.]1:[0-9]+)"))?.[1];
    if (baseUrl) {
      try { if ((await fetch(baseUrl + "/v1/health", { signal: globalThis.AbortSignal.timeout(1000) })).ok) return baseUrl; }
      catch { /* 服务尚未监听，继续健康探测。 */ }
    }
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
  }
  throw new Error("独立测试服务启动超时：" + diagnostics);
}
try {
  const baseUrl = await ready();
  console.log("[integration] 测试证据目录：" + evidence);
  const result = spawn(process.execPath, ["--test", "tests/product-cloud-live.test.ts", "tests/sp3-4-ledger-store.test.ts",
    "tests/sp3-5-cloud-live.test.ts", "tests/sp3-5-cloud-business.test.ts"], {
    cwd: clientRoot, stdio: "inherit", windowsHide: true,
    env: { ...process.env, ERDOS_API_BASE_URL: baseUrl, ERDOS_CLIENT_TEST_API_URL: baseUrl },
  });
  const exitCode = await new Promise((resolveExit, reject) => {
    result.once("error", reject); result.once("exit", code => resolveExit(code ?? 1));
  });
  process.exitCode = exitCode;
} finally {
  // 只结束本次 spawn 的进程树，临时证据保留以供核验。
  if (server.pid && server.exitCode === null) {
    if (process.platform === "win32") await new Promise(resolveStop => {
      execFile("taskkill", ["/PID", String(server.pid), "/T", "/F"], { windowsHide: true }, () => resolveStop());
    });
    else server.kill("SIGTERM");
  }
}
