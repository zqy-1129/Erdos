/**
 * W16 兼容矩阵预置（FE-COMPAT，客户端方案 §8）：本机可自动化维度的一键实测，
 * 输出供 docs/acceptance/fe-compat-2026-11.md 记录（11 月首轮与 12 月 SP3-7 复跑同源复跑）。
 *
 * 覆盖（自动化维度；级别口径与 demo-preflight 一致：FAIL=阻断交付 / WARN=记录复核 / INFO=矩阵数据）：
 *   ① 环境信息（OS / 内存 / 分辨率 / DPI 缩放 / 杀软）                         INFO
 *   ② 本地化·中文路径全链路（导入→执行→导出；含空格+中文目录）                 FAIL
 *   ③ 低配·冷启动度量（BootTimer 口径 + 进程级墙钟；默认 5 采样）              WARN（预算 <3s）
 *   ④ 网络·离线/弱网资产复跑（72h 宽限 + 遥测 outbox 既有测试资产）            FAIL
 * 手动维度（Win10 实机 / 多分辨率截图走查 / 4GB 低配机 / 第三方杀软 / 中文用户名账户）
 * 不在脚本内，见 docs/acceptance/fe-compat-2026-11.md 的 11 月执行清单。
 *
 * 隔离：全部使用临时目录（中文+空格命名）作 userData / 引擎 home，不触碰真实用户数据。
 * 用法（client/ 目录）：node scripts/compat-check.mjs [--out <证据JSON>] [--boot-samples N]
 * 退出码：0=无阻断项（含仅 WARN/INFO）；1=存在 FAIL。
 */
import { execSync, spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { cpus, tmpdir, totalmem } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { complianceSaveFromEngineTrail, openReadOnly } from "../main/engine-trail.ts";
import { readLocalHistory } from "../main/local-history.ts";

const require = createRequire(import.meta.url);
const electronPath = require("electron"); // 纯 Node 环境下要求结果为可执行文件路径（标准用法）
const clientRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(clientRoot, "..");

// ---- CLI 参数 --------------------------------------------------------------
const argv = process.argv.slice(2);
const argValue = (name) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : null);
const outPath = argValue("--out");
const bootSamples = Math.max(1, Number(argValue("--boot-samples") ?? "5") || 5);

// ---- 结果收集（逐项输出 + 汇总退出码；证据 JSON 汇总 payload） --------------
const results = [];
const payload = {};
const record = (level, label, detail) => {
  results.push({ level, label, detail });
  const icon = level === "FAIL" ? "✘" : level === "WARN" ? "⚠" : level === "INFO" ? "ℹ" : "✔";
  console.log(`  ${icon} ${label}${detail ? ` — ${detail}` : ""}`);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** 单行截断（失败归因用；多行压成竖线分隔）。 */
const tail = (text, n = 240) => {
  const flat = String(text ?? "").trim().split(/\r?\n/).join(" | ");
  return flat.length > n ? `…${flat.slice(-n)}` : flat;
};

// ---- 前置：引擎解释器（与 demo-preflight / smoke-electron 同口径） ----------
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

// ---- ① 环境信息（矩阵文档记录用；win32 经 PowerShell 采集深入项） -----------
function collectEnvironment() {
  const info = {
    platform: `${process.platform} ${process.arch}`,
    node: process.version,
    cpu: `${(cpus()[0]?.model ?? "未知").trim()} × ${cpus().length}`,
    ramGB: Math.round((totalmem() / 1024 ** 3) * 10) / 10,
  };
  if (process.platform === "win32") {
    const ps = [
      "$os = Get-CimInstance Win32_OperatingSystem;",
      "Write-Output ('caption=' + $os.Caption);",
      "Write-Output ('version=' + $os.Version);",
      "$vc = Get-CimInstance Win32_VideoController | Select-Object -First 1;",
      "Write-Output ('resolution=' + $vc.CurrentHorizontalResolution + 'x' + $vc.CurrentVerticalResolution);",
      "$lp = (Get-ItemProperty 'HKCU:\\Control Panel\\Desktop' -ErrorAction SilentlyContinue).LogPixels;",
      "if (-not $lp) { $lp = 96 };",
      "Write-Output ('dpiPercent=' + [math]::Round($lp / 96 * 100));",
      "try { $av = (Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop |",
      "ForEach-Object { $_.displayName }) -join ';'; Write-Output ('antivirus=' + $av) }",
      "catch { Write-Output 'antivirus=未获取' }",
    ].join(" ");
    try {
      const out = execSync(`powershell -NoProfile -Command "${ps}"`, {
        stdio: ["ignore", "pipe", "ignore"],
        timeout: 20_000,
      }).toString();
      for (const line of out.split(/\r?\n/)) {
        const sep = line.indexOf("=");
        if (sep > 0) info[line.slice(0, sep).trim()] = line.slice(sep + 1).trim();
      }
    } catch {
      info.note = "PowerShell 信息采集失败（caption/resolution/dpiPercent/antivirus 未获取）";
    }
  }
  return info;
}

// ---- ② 本地化·中文路径全链路（导入→执行→导出） -----------------------------
async function checkLocalization(enginePython) {
  if (enginePython === null) {
    record("FAIL", "② 本地化·中文路径", "未找到引擎环境（engine/.venv）；可用 ERDOS_ENGINE_PYTHON 指定");
    return;
  }
  // 中文 + 空格目录经受全链：userData / 引擎 home / 冒烟证据 / 导出文件
  const base = mkdtempSync(join(tmpdir(), "erdos-兼容 预检-"));
  const userData = join(base, "用户数据 userdata");
  const home = join(base, "引擎数据 engine-home");
  const evidencePath = join(base, "冒烟证据.json");
  const exportDir = join(base, "导出 exports");
  for (const dir of [userData, home, exportDir]) mkdirSync(dir, { recursive: true });
  payload.localization = { base, home };

  // ②a 导入+执行：真实壳级冒烟（引擎 spawn / NDJSON / SQLite / 中文字段全链）
  const smoke = spawnSync(electronPath, [clientRoot], {
    env: {
      ...process.env,
      ERDOS_SMOKE: "1",
      ERDOS_SMOKE_OUT: evidencePath,
      ERDOS_SMOKE_HOME: home,
      ERDOS_ENGINE_CMD: enginePython,
      ERDOS_CHANNEL: "dev", // 冒烟不触发自动更新（渠道门禁）
      ERDOS_USER_DATA_DIR: userData,
    },
    stdio: ["ignore", "pipe", "pipe"],
    encoding: "utf-8",
    timeout: 300_000,
  });
  if (!existsSync(evidencePath)) {
    record(
      "FAIL",
      "②a 中文路径·壳级冒烟",
      `未产出证据（electron 退出码 ${smoke.status ?? "?"}）：${tail(smoke.stderr)}`,
    );
    return; // 后续回读/导出依赖冒烟留痕，跳过
  }
  const evidence = JSON.parse(readFileSync(evidencePath, "utf-8"));
  if (!evidence.ok || smoke.status !== 0) {
    const failed = (evidence.steps ?? []).filter((step) => !step.ok).map((step) => step.name).join("；");
    record("FAIL", "②a 中文路径·壳级冒烟", `失败步骤：${failed || "未知"}；${tail(smoke.stderr)}`);
    return;
  }
  payload.localization.smoke = { home, states: evidence.states, steps: evidence.steps.length };
  record("PASS", "②a 中文路径·壳级冒烟", `中文+空格目录下四阶段全链通过（证据 ${basename(evidencePath)}）`);

  // ②b 数据回读：留痕×检查点组合读取 + 中文题面字节往返（直读 task_inputs）
  try {
    const tasks = readLocalHistory(join(home, "audit.db"), join(home, "checkpoints.db"));
    const task = tasks.find((item) => item.taskId === "smoke-task");
    const db = openReadOnly(join(home, "checkpoints.db"));
    let problemText = "";
    try {
      const row = db.prepare("SELECT problem_text FROM task_inputs WHERE task_id = ?").get("smoke-task");
      problemText = String(row?.problem_text ?? "");
    } finally {
      db.close();
    }
    const titleOk = task?.title === "壳级冒烟";
    const textOk = problemText.includes("冒烟题面：计算 1+1");
    payload.localization.readback = { found: task !== undefined, title: task?.title ?? null, textOk };
    if (!task) {
      record("FAIL", "②b 中文路径·数据回读", "留痕列表未找到 smoke-task");
    } else {
      record(
        titleOk && textOk ? "PASS" : "FAIL",
        "②b 中文路径·数据回读",
        `任务 ${task.taskId}；标题「${task.title}」${titleOk ? "" : "（应为「壳级冒烟」）"}；题面中文字节往返 ${textOk ? "完好" : "异常"}`,
      );
    }
  } catch (error) {
    record("FAIL", "②b 中文路径·数据回读", `读取失败：${error instanceof Error ? error.message : String(error)}`);
  }

  // ②c 导出落盘：声明真实生成（md 文本 + docx zip 二进制）并写入中文路径；
  // 保存器注入的写盘语义与生产 createFileSaver 一致（对话框选路径 → writeFile）
  try {
    const auditDb = join(home, "audit.db");
    const mdPath = join(exportDir, "AI 使用声明.md");
    const docxPath = join(exportDir, "AI 使用声明.docx");
    const saverFor = (target) => async (request) => {
      writeFileSync(target, request.content);
      return { canceled: false, path: target };
    };
    const options = { taskId: "smoke-task", humanNote: "兼容预检（中文路径）", unusedAi: false };
    await complianceSaveFromEngineTrail(auditDb, { ...options, format: "md" }, saverFor(mdPath));
    await complianceSaveFromEngineTrail(auditDb, { ...options, format: "docx" }, saverFor(docxPath));
    const mdBytes = readFileSync(mdPath);
    const docxBytes = readFileSync(docxPath);
    const zipMagic =
      docxBytes[0] === 0x50 && docxBytes[1] === 0x4b && docxBytes[2] === 0x03 && docxBytes[3] === 0x04;
    const mdOk = mdBytes.length > 100 && mdBytes.toString("utf-8").includes("声明");
    payload.localization.export = {
      mdPath,
      docxPath,
      mdBytes: mdBytes.length,
      docxBytes: docxBytes.length,
      zipMagic,
    };
    record(
      mdOk && zipMagic ? "PASS" : "FAIL",
      "②c 中文路径·声明导出（md+docx）",
      `md ${mdBytes.length}B${mdOk ? "" : "（内容断言未过）"}；docx ${docxBytes.length}B（zip 魔数 ${zipMagic ? "✓" : "✘"}）`,
    );
  } catch (error) {
    record("FAIL", "②c 中文路径·声明导出（md+docx）", `导出失败：${error instanceof Error ? error.message : String(error)}`);
  }

  // 临时目录回收：全部通过则清理；存在 FAIL 保留现场（含冒烟证据与导出样本，供归因）
  const locFailed = results.some((item) => item.label.startsWith("②") && item.level === "FAIL");
  payload.localization.cleaned = !locFailed;
  if (locFailed) {
    console.log(`    现场保留（含冒烟证据/导出样本）：${base}`);
  } else {
    rmSync(base, { recursive: true, force: true });
  }
}

// ---- ③ 低配·冷启动度量（BootTimer 口径 + 进程级墙钟） ----------------------
/** 单次冷启动采样：spawn 真实应用（隐藏窗口），捕获渲染层 [boot] 输出后回收。 */
function sampleBoot(userDataDir, timeoutMs = 25_000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(electronPath, [clientRoot], {
      env: {
        ...process.env,
        ERDOS_USER_DATA_DIR: userDataDir,
        ERDOS_BOOT_PROBE: "1", // 隐藏窗口（见 main/window.ts）：采样期间不闪窗
        ERDOS_CHANNEL: "dev",
        ELECTRON_ENABLE_LOGGING: "1", // 渲染层 console 经 Chromium 日志透传至 stderr
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let settled = false;
    let buffer = "";
    const onData = (chunk) => {
      buffer += chunk.toString("utf-8");
      const match = buffer.match(/\[boot\] first-render=([\d.]+)ms/);
      if (match && !settled) {
        finish({ appReadyMs: Number(match[1]), wallMs: Date.now() - started });
      }
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    child.stdout?.on("data", onData);
    child.stderr?.on("data", onData);
    child.once("error", () => finish(null));
    function finish(value) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      killTree(child);
      void Promise.race([once(child, "exit"), sleep(2000)]).then(() => resolve(value));
    }
  });
}

/** 回收应用进程（Windows 树杀；POSIX 先 SIGTERM 由系统清理）。 */
function killTree(child) {
  if (child.pid == null || child.exitCode !== null) return;
  if (process.platform === "win32") {
    try {
      execSync(`taskkill /PID ${child.pid} /T /F`, { stdio: "ignore" });
    } catch {
      // 进程已退出：忽略
    }
  } else {
    try {
      child.kill("SIGTERM");
    } catch {
      // 进程已退出：忽略
    }
  }
}

async function checkColdStart() {
  const base = mkdtempSync(join(tmpdir(), "erdos-兼容 冷启动-"));
  const userData = join(base, "用户数据 userdata");
  mkdirSync(userData, { recursive: true });
  const appReady = [];
  const wall = [];
  for (let index = 0; index < bootSamples; index += 1) {
    const sample = await sampleBoot(userData);
    if (sample !== null) {
      wall.push(sample.wallMs);
      if (Number.isFinite(sample.appReadyMs)) appReady.push(sample.appReadyMs);
    }
    console.log(
      `    · 采样 ${index + 1}/${bootSamples}：${
        sample === null ? "未捕获（超时）" : `BootTimer ${sample.appReadyMs}ms / 墙钟 ${sample.wallMs}ms`
      }`,
    );
    await sleep(300); // 进程回收缓冲
  }
  const values = appReady.length > 0 ? appReady : wall; // 未捕获 BootTimer 时兜底墙钟口径
  if (values.length === 0) {
    record("WARN", "③ 低配·冷启动", `全部采样未捕获（${bootSamples} 次）；建议复跑或人工观察`);
    payload.coldStart = { samples: wall, appReadyMs: appReady, captured: false, userData, cleaned: true };
    rmSync(base, { recursive: true, force: true });
    return;
  }
  const sorted = [...values].sort((a, b) => a - b);
  const p50 = sorted[Math.floor(sorted.length / 2)];
  const min = sorted[0];
  const max = sorted[sorted.length - 1];
  const budgetMs = 3000; // PRD 6.1 冷启动 <3s
  const within = p50 < budgetMs;
  record(
    within ? "PASS" : "WARN",
    "③ 低配·冷启动",
    `BootTimer 口径 p50=${p50}ms（min ${min} / max ${max}，n=${values.length}）${
      within ? "，预算 <3s 达标" : "，超出 <3s 预算（dev 未打包口径，记录复核）"
    }；墙钟 ${wall.join("/")}ms`,
  );
  // ③b 中文路径 userData 建库（正常启动链：SQLite 密钥库 + 设备指纹）
  const dbOk = existsSync(join(userData, "erdos.db"));
  record(dbOk ? "PASS" : "WARN", "③b 中文路径·userData 建库", `erdos.db ${dbOk ? "已生成" : "未生成（检查建库链）"}`);
  payload.coldStart = {
    appReadyMs: appReady,
    wallMs: wall,
    p50,
    min,
    max,
    budgetMs,
    captured: appReady.length > 0,
    userData,
    erdosDb: dbOk,
    cleaned: true,
  };
  rmSync(base, { recursive: true, force: true }); // 采样目录回收（无保留价值）
}

// ---- ④ 网络·离线/弱网资产复跑（既有测试资产，方案 §8 口径） ----------------
function checkOfflineAssets() {
  const files = ["tests/sp3-4-entitlement-store.test.ts", "tests/sp3-5-telemetry.test.ts"];
  const run = spawnSync(process.execPath, ["--test", ...files], {
    cwd: clientRoot,
    stdio: ["ignore", "pipe", "pipe"],
    encoding: "utf-8",
    timeout: 180_000,
  });
  const output = `${run.stdout ?? ""}${run.stderr ?? ""}`;
  const count = (pattern) => Number(output.match(pattern)?.[1] ?? -1);
  const pass = count(/(?:ℹ|#) pass (\d+)/);
  const fail = count(/(?:ℹ|#) fail (\d+)/);
  payload.offlineAssets = { files, pass, fail, status: run.status };
  if (run.status === 0 && fail === 0) {
    record("PASS", "④ 网络·离线/弱网资产复跑", `72h 宽限 + 遥测 outbox 用例 ${pass} 例全过`);
  } else if (run.status === 0) {
    // 退出码 0 但计数解析失败：不放行也不阻断（人工复核输出）
    record("WARN", "④ 网络·离线/弱网资产复跑", `退出码 0 但计数解析失败（pass=${pass} fail=${fail}）；请人工复核输出`);
  } else {
    record("FAIL", "④ 网络·离线/弱网资产复跑", `退出码 ${run.status ?? "?"}；pass=${pass} fail=${fail}；${tail(output)}`);
  }
}

// ---- 主流程 ----------------------------------------------------------------
console.log("[compat] W16 兼容矩阵预检（本机自动化维度：环境 / 中文路径 / 冷启动 / 离线资产）");
const enginePython = resolveEnginePython();

console.log("■ ① 环境信息");
const environment = collectEnvironment();
payload.environment = environment;
record(
  "INFO",
  "① 环境信息",
  `${environment.caption ?? environment.platform}；RAM ${environment.ramGB}GB；分辨率 ${environment.resolution ?? "未知"}；DPI ${environment.dpiPercent ? `${environment.dpiPercent}%` : "未知"}；杀软 ${environment.antivirus ?? "未获取"}`,
);

console.log("■ ② 本地化·中文路径全链路（导入→执行→导出）");
await checkLocalization(enginePython);

console.log("■ ③ 低配·冷启动度量");
await checkColdStart();

console.log("■ ④ 网络·离线/弱网资产复跑");
checkOfflineAssets();

// ---- 汇总 ------------------------------------------------------------------
const failed = results.filter((item) => item.level === "FAIL").length;
const warned = results.filter((item) => item.level === "WARN").length;
console.log(
  `\n[compat] 结果：${failed > 0 ? "存在阻断项" : "无阻断项"}（通过 ${results.filter((i) => i.level === "PASS").length} / 提示 ${warned} / 阻断 ${failed}）`,
);
if (outPath) {
  writeFileSync(outPath, JSON.stringify({ generatedAt: new Date().toISOString(), results, payload }, null, 2), "utf-8");
  console.log(`[compat] 证据 → ${outPath}`);
}
process.exit(failed > 0 ? 1 : 0);