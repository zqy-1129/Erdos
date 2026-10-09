/**
 * C0 演示（10-31）环境预检：演示前一键自检，提前暴露环境问题（可复跑、结果可进演示记录）。
 *
 * 检查项（PASS=正常；FAIL=阻断演示；WARN=仅提示降级预案；INFO=参考信息）：
 *   ① 引擎环境（engine/.venv 或 ERDOS_ENGINE_PYTHON + `-m engine --version`）        FAIL
 *   ② 客户端构建产物（dist/index.html + dist/main/index.js + dist/main/preload.cjs） FAIL
 *   ③ 沙箱镜像预热（docker image inspect <ERDOS_SANDBOX_IMAGE|python:3.12-slim>；冷启动拖慢 solving） WARN
 *   ④ 服务端健康（GET <ERDOS_API_BASE_URL|http://127.0.0.1:8000>/v1/health）         WARN
 *   ⑤ 导出落盘探针（系统临时目录写→删；演示导出链路前置）                            FAIL
 *   ⑥ 旧实例占用（electron 进程数；演示前建议关闭旧实例）                            WARN
 *   ⑦ 历史页数据（引擎数据目录派生同客户端口径：ERDOS_ENGINE_HOME → ERDOS_USER_DATA_DIR
 *      → 默认 userData/<name>，其下 engine-home 留痕/检查点任务数；供「断点续跑」演示幕） INFO/WARN
 *
 * 用法（client/ 目录）：node scripts/demo-preflight.mjs
 * 退出码：0=无阻断项（含仅 WARN）；1=存在 FAIL。
 */
import { execSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { readLocalHistory } from "../main/local-history.ts";

const clientRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(clientRoot, "..");

/** 检查结果收集（逐项输出 + 汇总退出码）。 */
const results = [];
const record = (level, label, detail) => {
  results.push({ level, label, detail });
  const icon = level === "FAIL" ? "✘" : level === "WARN" ? "⚠" : level === "INFO" ? "ℹ" : "✔";
  console.log(`  ${icon} ${label}${detail ? ` — ${detail}` : ""}`);
};
const runQuiet = (command, options = {}) =>
  execSync(command, { cwd: repoRoot, stdio: ["ignore", "pipe", "ignore"], timeout: 15_000, ...options })
    .toString()
    .trim();

// ① 引擎环境
function resolveEnginePython() {
  if (process.env.ERDOS_ENGINE_PYTHON) return process.env.ERDOS_ENGINE_PYTHON;
  for (const candidate of [
    join(repoRoot, "engine", ".venv", "Scripts", "python.exe"),
    join(repoRoot, "engine", ".venv", "bin", "python"),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}
const enginePython = resolveEnginePython();
if (enginePython === null) {
  record("FAIL", "① 引擎环境", "未找到 engine/.venv；可用 ERDOS_ENGINE_PYTHON 指定解释器");
} else {
  try {
    const version = runQuiet(`"${enginePython}" -m engine --version`);
    record("PASS", "① 引擎环境", `${basename(enginePython)} · ${version}`);
  } catch (error) {
    record("FAIL", "① 引擎环境", `引擎 --version 失败：${error instanceof Error ? error.message : String(error)}`);
  }
}

// ② 构建产物
const distFiles = ["dist/index.html", "dist/main/index.js", "dist/main/preload.cjs"].map((file) =>
  join(clientRoot, file),
);
const missing = distFiles.filter((file) => !existsSync(file));
if (missing.length === 0) {
  record("PASS", "② 客户端构建产物", "dist/index.html + dist/main/{index.js,preload.cjs}");
} else {
  record("FAIL", "② 客户端构建产物", `缺失 ${missing.length} 个（先执行 npm run build）：${missing.map((f) => f.split("dist/")[1]).join(", ")}`);
}

// ③ 沙箱镜像预热（冷启动拉取会拖慢 solving 演示）
const sandboxImage = process.env.ERDOS_SANDBOX_IMAGE ?? "python:3.12-slim";
try {
  runQuiet(`docker image inspect ${sandboxImage}`);
  record("PASS", "③ 沙箱镜像预热", `${sandboxImage} 已就绪`);
} catch (error) {
  const reason = error instanceof Error ? error.message.split("\n")[0] : String(error);
  record("WARN", "③ 沙箱镜像预热", `docker 不可用或镜像未就绪（${reason.slice(0, 80)}）；演示前建议 docker pull ${sandboxImage}`);
}

// ④ 服务端健康（与客户端同一环境变量口径 ERDOS_API_BASE_URL；不可达时按走查清单 §3 降级）
const apiBase = process.env.ERDOS_API_BASE_URL ?? "http://127.0.0.1:8000";
const controller = new AbortController();
const healthTimer = setTimeout(() => controller.abort(), 2_500);
try {
  const response = await fetch(`${apiBase}/v1/health`, { signal: controller.signal });
  if (response.ok) {
    record("PASS", "④ 服务端健康", `${apiBase}/v1/health → ${response.status}`);
  } else {
    record("WARN", "④ 服务端健康", `${apiBase}/v1/health → ${response.status}（非 200；登录/账单可能降级）`);
  }
} catch {
  record("WARN", "④ 服务端健康", `${apiBase} 不可达；启动 uvicorn 或采用降级预案（走查清单 §3）`);
} finally {
  clearTimeout(healthTimer);
}

// ⑤ 导出落盘探针
try {
  const probeDir = mkdtempSync(join(tmpdir(), "erdos-demo-preflight-"));
  const probeFile = join(probeDir, "probe.txt");
  writeFileSync(probeFile, "ok", "utf-8");
  rmSync(probeDir, { recursive: true, force: true });
  record("PASS", "⑤ 导出落盘探针", "系统临时目录可写可删");
} catch (error) {
  record("FAIL", "⑤ 导出落盘探针", `临时目录不可写：${error instanceof Error ? error.message : String(error)}`);
}

// ⑥ 旧实例占用（提示关闭；不影响单实例正确性）
try {
  // win32：tasklist 逐行输出进程；POSIX：pgrep -c 输出裸计数（`|| true` 吞掉无匹配的非零退出码）
  const listing =
    process.platform === "win32"
      ? runQuiet('tasklist /FI "IMAGENAME eq electron.exe" /NH')
      : runQuiet("pgrep -c electron || true");
  const count =
    process.platform === "win32"
      ? listing.split(/\r?\n/).filter((line) => /electron/i.test(line)).length
      : Number.parseInt(listing.trim(), 10) || 0;
  if (count > 0) {
    record("WARN", "⑥ 旧实例占用", `检测到 electron 相关进程 ${count} 个；演示前建议关闭旧实例（避免窗口/单实例混淆）`);
  } else {
    record("PASS", "⑥ 旧实例占用", "无 electron 进程");
  }
} catch {
  record("INFO", "⑥ 旧实例占用", "进程枚举不可用（跳过）");
}

// ⑦ 历史页数据（为「断点续跑」演示幕预热）
/** 引擎数据目录：与客户端 index.ts 同一口径（userData/engine-home；ERDOS_ENGINE_HOME 优先）。 */
function resolveEngineHome() {
  if (process.env.ERDOS_ENGINE_HOME) return process.env.ERDOS_ENGINE_HOME;
  const userData =
    process.env.ERDOS_USER_DATA_DIR ??
    join(
      process.platform === "win32"
        ? (process.env.APPDATA ?? join(homedir(), "AppData", "Roaming"))
        : process.platform === "darwin"
          ? join(homedir(), "Library", "Application Support")
          : join(homedir(), ".config"),
      "erdos-client", // Electron 默认 userData 名 = package.json name
    );
  return join(userData, "engine-home");
}
const engineHome = resolveEngineHome();
if (!existsSync(engineHome)) {
  record("INFO", "⑦ 历史页数据", `引擎数据目录不存在（${engineHome}）；先在客户端跑一次任务后再检`);
} else {
  try {
    const tasks = readLocalHistory(join(engineHome, "audit.db"), join(engineHome, "checkpoints.db"));
    const resumable = tasks.filter((task) => task.resumable).length;
    record(
      tasks.length > 0 ? "PASS" : "WARN",
      "⑦ 历史页数据",
      `本地历史 ${tasks.length} 条（可续 ${resumable} 条）${tasks.length === 0 ? "；建议先跑一次任务以演示续跑" : ""}`,
    );
  } catch (error) {
    record("WARN", "⑦ 历史页数据", `本地历史读取异常：${error instanceof Error ? error.message : String(error)}`);
  }
}

// 汇总
const failed = results.filter((item) => item.level === "FAIL").length;
const warned = results.filter((item) => item.level === "WARN").length;
console.log(
  `\n[preflight] 结果：${failed > 0 ? "存在阻断项" : "无阻断项"}（通过 ${results.filter((i) => i.level === "PASS").length} / 提示 ${warned} / 阻断 ${failed}）`,
);
process.exit(failed > 0 ? 1 : 0);