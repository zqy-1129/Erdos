/**
 * 壳级冒烟执行器（FE-HOST W3 验收预演 / 10-24 J-1024 前置）：
 * 以 ERDOS_SMOKE=1 启动 Electron 主进程（无窗口），跑通
 * 「引擎启动 → initialize 握手 → task_create → 四阶段（门禁自动通过）→ 论文产物」，
 * 读取证据 JSON 汇总并透出退出码。
 *
 * 前置：
 * - 已构建：`npm run build`（dist/main/index.js 就位）；
 * - 引擎环境：engine/.venv（或 ERDOS_ENGINE_PYTHON 覆盖）；无 Key → FakeLLM 确定性模式。
 *
 * 用法：node scripts/smoke-electron.mjs
 * 退出码：0=通过；1=冒烟失败；2=前置缺失（未构建 / 无引擎环境）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const clientRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(clientRoot, "..");

// ---- 前置检查 -------------------------------------------------------------
const mainEntry = join(clientRoot, "dist", "main", "index.js");
if (!existsSync(mainEntry)) {
  console.error("未构建：请先在 client/ 执行 `npm run build`（缺少 dist/main/index.js）");
  process.exit(2);
}

/** 解析引擎可执行（与 tests/engine-host.test.ts 同口径）。 */
function resolveEnginePython() {
  if (process.env.ERDOS_ENGINE_PYTHON) return process.env.ERDOS_ENGINE_PYTHON;
  const candidates = [
    join(repoRoot, "engine", ".venv", "Scripts", "python.exe"), // Windows
    join(repoRoot, "engine", ".venv", "bin", "python"), // POSIX
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

const enginePython = resolveEnginePython();
if (enginePython === null) {
  console.error("未找到引擎环境（engine/.venv）：可用 ERDOS_ENGINE_PYTHON 指定解释器");
  process.exit(2);
}

// ---- 执行冒烟 -------------------------------------------------------------
// electron 包在纯 Node 环境下 require 结果为可执行文件路径（标准用法）
const electronPath = require("electron");
const outDir = mkdtempSync(join(tmpdir(), "erdos-smoke-"));
const outPath = join(outDir, "evidence.json");

console.log(`[smoke] 启动 Electron 壳级冒烟（无窗口）→ 证据：${outPath}`);
const result = spawnSync(electronPath, [clientRoot], {
  env: {
    ...process.env,
    ERDOS_SMOKE: "1",
    ERDOS_SMOKE_OUT: outPath,
    ERDOS_ENGINE_CMD: enginePython, // 主进程 engineCommand() 优先读取该变量
    ERDOS_CHANNEL: "dev", // 冒烟不触发自动更新（渠道门禁）
  },
  stdio: "inherit",
  timeout: 300_000,
});

// ---- 汇总 -----------------------------------------------------------------
if (!existsSync(outPath)) {
  console.error(`[smoke] 未产出证据文件（electron 退出码 ${result.status ?? "?"}，可能启动失败）`);
  process.exit(1);
}

const evidence = JSON.parse(readFileSync(outPath, "utf-8"));
console.log("\n[smoke] 步骤明细：");
for (const step of evidence.steps) {
  const flag = step.ok ? "✔" : "✘";
  console.log(`  ${flag} ${step.name}（${step.ms}ms）${step.detail ? ` — ${step.detail}` : ""}`);
}
console.log(
  `[smoke] 事件统计：共 ${evidence.eventCount} 条 ${JSON.stringify(evidence.eventsByKind)}；` +
    `状态轨迹：${evidence.states.join(" → ")}`,
);
if (evidence.error) console.error(`[smoke] 异常：${evidence.error}`);

if (!evidence.ok || result.status !== 0) {
  console.error("[smoke] 结果：失败（见上方失败步骤与证据 JSON）");
  process.exit(1);
}
console.log("[smoke] 结果：通过（握手 / 四阶段推进 / 门禁 / 论文产物 / 优雅退出）");