/**
 * W6 · J-1024 三进程汇合联调预演（10-24 硬门前置，可无人值守重复执行）。
 *
 * 覆盖联调清单（对齐手册 W6 步骤①②，除真实 Key 通道外全部脚本化）：
 *   ⓪ 契约清单核对（方法：schema ↔ client ↔ engine 三方；事件：schema ↔ client，引擎侧由
 *     engine/tests/test_contract_alignment.py 守护——本批已实测 4 例通过）
 *   ① 引擎启动与握手（spawn → initialize → ready，EC-T6 ≤10s）
 *   ② task_create 登记题面
 *   ③ 事件分流（analysis 完成；RPC 层未知事件零计数——未知事件在 rpc.ts 拒绝并计入协议错误）
 *   ④ 门禁 gate_analysis 通过（answer_gate）
 *   ⑤ kill 看门狗（强杀引擎 → crashed → 自动重启 → ready；重启次数由状态轨迹证实）
 *   ⑥ 崩溃后无丢任务（重启后补投题面 + 检查点延续 modeling 完成）
 *   ⑦ 退出链（stop → 无遗留进程，协议硬上限 300ms；Windows SIGTERM=硬终止语义见风险登记）
 *   ⑧ 检查点与留痕完好（node:sqlite 读 checkpoints.db 行 + 客户端读取器读 audit.db；
 *     崩溃前留痕不得减少）
 *   ⑨ 单实例（同一 userData：第二实例被锁拒绝且未进入 boot；控制组：不同 userData 可正常启动）
 *
 * 用法（client/ 目录）：
 *   node scripts/w6-drill.mjs [--skip-singleton] [--skip-engine] [--out <evidence.json>]
 * 前置：engine/.venv（或 ERDOS_ENGINE_PYTHON 覆盖）；⑨ 需已构建 dist（npm run build）。
 * 退出码：0=全项通过；1=存在失败步骤；2=前置缺失。
 * 证据：JSON 默认写 reports/w6-drill-<日期>.json（steps/状态轨迹/事件统计/版本/计时；
 * 路径已脱敏为 basename，完整 home 见控制台输出）。
 */
import { execSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";

import { EngineHost } from "../main/engine-host/host.ts";
import { readRecentTasks } from "../main/engine-trail.ts";
import { ENGINE_EVENT_NAMES, RPC_METHODS } from "../shared/ipc.ts";

const require = createRequire(import.meta.url);
const clientRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(clientRoot, "..");

// ---- 参数与前置 -----------------------------------------------------------
const argv = process.argv.slice(2);
const skipSingleton = argv.includes("--skip-singleton");
const skipEngine = argv.includes("--skip-engine");
const outIndex = argv.indexOf("--out");
const stamp = new Date().toISOString().slice(0, 10);
const outPath = outIndex >= 0 && argv[outIndex + 1] ? argv[outIndex + 1] : join(repoRoot, "reports", `w6-drill-${stamp}.json`);

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
if (!skipEngine && enginePython === null) {
  console.error("未找到引擎环境（engine/.venv）：可用 ERDOS_ENGINE_PYTHON 指定解释器");
  process.exit(2);
}
const mainEntry = join(clientRoot, "dist", "main", "index.js");
const canRunSingleton = !skipSingleton && existsSync(mainEntry);
if (!skipSingleton && !existsSync(mainEntry)) {
  console.warn("[drill] 未构建 dist/main（跳过单实例步骤）：先执行 npm run build");
}

// ---- 公共工具 -------------------------------------------------------------
const DRILL_TASK = "w6-drill-task";
const home = mkdtempSync(join(tmpdir(), "erdos-w6-drill-"));
const steps = [];
const states = [];
const events = [];
const protocolErrors = [];
let mark = Date.now();
/** 步骤记录：skipped 步骤不计入通过率（跳过即跳过，不冒充通过）。 */
const step = (name, ok, detail = "", skipped = false) => {
  steps.push({ name, ok: skipped ? true : ok, skipped, detail, ms: Date.now() - mark });
  mark = Date.now();
  const flag = skipped ? "○" : ok ? "✔" : "✘";
  console.log(`  ${flag} ${name}${detail ? ` — ${detail}` : ""}${skipped ? "（跳过）" : ""}`);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitUntil(condition, timeoutMs) {
  const start = Date.now();
  while (!condition()) {
    if (Date.now() - start > timeoutMs) return false;
    await sleep(25);
  }
  return true;
}
/** 强杀进程树（模拟崩溃）：Windows taskkill /T /F；POSIX SIGKILL。已退出则忽略。 */
function killAbrupt(pid) {
  try {
    if (process.platform === "win32") execSync(`taskkill /PID ${pid} /T /F`, { stdio: "ignore" });
    else process.kill(pid, "SIGKILL");
  } catch {
    /* 进程已退出 */
  }
}
function killTree(proc) {
  if (proc && proc.exitCode === null && proc.pid != null) {
    try {
      if (process.platform === "win32") execSync(`taskkill /PID ${proc.pid} /T /F`, { stdio: "ignore" });
      else proc.kill("SIGTERM");
    } catch {
      /* 已退出 */
    }
  }
}

// ---- ⓪ 契约清单核对（W6 步骤②文档走查的机器化部分） ------------------------
function runContractCheck() {
  try {
    const schema = JSON.parse(readFileSync(join(repoRoot, "contracts", "engine-rpc.schema.json"), "utf8"));
    const schemaMethods = Object.keys(schema.methods ?? {}).sort();
    const schemaEvents = Object.keys(schema.events ?? {}).sort();
    const clientMethods = [...RPC_METHODS].sort();
    const clientEvents = [...ENGINE_EVENT_NAMES].sort();
    const engineSrc = readFileSync(join(repoRoot, "engine", "ipc", "methods.py"), "utf8");
    const engineMethods = [...engineSrc.matchAll(/server\.register\("([a-z_.]+)"/g)].map((m) => m[1]).sort();
    const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
    const methodsAligned = same(schemaMethods, clientMethods) && same(schemaMethods, engineMethods);
    const eventsAligned = same(schemaEvents, clientEvents);
    step(
      "⓪ 契约清单核对（方法三方 / 事件两侧）",
      methodsAligned && eventsAligned,
      `方法 ${schemaMethods.length} 个（schema/client/engine 三方一致=${methodsAligned}）；` +
        `事件 ${schemaEvents.length} 个（schema↔client 一致=${eventsAligned}；引擎侧由 test_contract_alignment 守护）`,
    );
  } catch (err) {
    step("⓪ 契约清单核对", false, err instanceof Error ? err.message : String(err));
  }
}

// ---- ①~⑧ 引擎链路（真实引擎 + EngineHost 看门狗/检查点） -------------------
function makeHost() {
  return new EngineHost({
    command: enginePython,
    args: ["-m", "engine"],
    cwd: repoRoot,
    home,
    key: () => null, // 无 Key 模式：首行空行 → FakeLLM（演示/预演确定性）
    onEvent: (event) => {
      if (events.length < 5000) events.push(event);
    },
    onStateChange: (state) => states.push(state),
    onLog: (line) => console.log(`[engine] ${line}`),
    onProtocolError: (err) => {
      protocolErrors.push(err.message);
      console.warn(`[drill] 协议错误：${err.message}`);
    },
  });
}

async function runEngineSection() {
  const host = makeHost();
  /** 崩溃前已落盘的留痕条数（崩溃后不得减少——「无静默丢任务」数据侧不变量）。 */
  let trailBeforeCrash = 0;
  try {
    // ① 启动 + 握手
    const t0 = Date.now();
    await host.reloadKey();
    const readyMs = Date.now() - t0;
    step(
      "① 引擎启动与握手（spawn → initialize → ready）",
      host.currentState === "ready" && readyMs <= 10_000,
      `state=${host.currentState}；耗时 ${readyMs}ms（EC-T6 上限 10s）`,
    );

    // ② 题面登记
    const created = await host.invoke("task_create", {
      task_id: DRILL_TASK,
      title: "W6 联调预演",
      problem_text: "预演题面：计算 1+1（无 Key FakeLLM 确定性模式，不发起真实模型调用）",
    });
    step("② task_create 登记题面", created?.status === "created", JSON.stringify(created));

    // ③ 事件分流（analysis 完成 + RPC 层未知事件零计数）
    const beforeAnalysis = events.length;
    await host.invoke("start_stage", { task_id: DRILL_TASK, stage: "analysis" });
    const progressed = await waitUntil(
      () => events.some((e) => e.event === "stage.progress" && e.stage === "analysis" && e.progress >= 1),
      60_000,
    );
    const unknownEvents = protocolErrors.filter((message) => message.includes("未知事件")).length;
    const kinds = [...new Set(events.map((e) => e.event))];
    step(
      "③ 事件分流（analysis 完成；RPC 层未知事件零计数）",
      progressed && unknownEvents === 0,
      `新增 ${events.length - beforeAnalysis} 条；种类 ${kinds.join(",")}；未知 ${unknownEvents}（rpc.ts 拒绝并计数）`,
    );

    // ④ 门禁通过（推进 stage_index）
    const gate = await host.invoke("answer_gate", { task_id: DRILL_TASK, gate: "gate_analysis", decision: "pass" });
    step("④ 门禁 gate_analysis 通过（answer_gate）", gate?.action === "next_stage", JSON.stringify(gate));

    // ⑤ kill 看门狗（强杀进程树 → crashed → 自动重启 → ready）
    // 崩溃前留痕基线：紧邻强杀前读取（覆盖到最后一刻的落盘）
    trailBeforeCrash = readRecentTasks(join(home, "audit.db"), 10).find((task) => task.taskId === DRILL_TASK)?.eventCount ?? 0;
    const pidBefore = host.childPid;
    if (pidBefore === null) throw new Error("引擎 pid 缺失：无法执行 kill 看门狗步骤");
    killAbrupt(pidBefore);
    const sawCrashed = await waitUntil(() => host.currentState === "crashed", 5_000);
    const backToReady = await waitUntil(
      () => host.currentState === "ready" && host.childPid !== null && host.childPid !== pidBefore,
      20_000,
    );
    step(
      "⑤ kill 看门狗（强杀 → crashed → 自动重启 ready）",
      sawCrashed && backToReady,
      `pid ${pidBefore} → ${host.childPid}；轨迹 ${states.join("→")}`,
    );

    // ⑥ 崩溃后无丢任务（重启后补投题面 + 检查点延续 modeling）
    // 引擎题面为内存态（不随检查点持久化）：崩溃重启后须补投题面——
    // 该口径已登记为 W6 风险项（客户端需持久化题面或历史任务来源补投）。
    await host.invoke("task_create", {
      task_id: DRILL_TASK,
      title: "W6 联调预演",
      problem_text: "预演题面：计算 1+1（重启后补投）",
    });
    await host.invoke("start_stage", { task_id: DRILL_TASK, stage: "modeling" });
    const modelingDone = await waitUntil(
      () => events.some((e) => e.event === "stage.progress" && e.stage === "modeling" && e.progress >= 1),
      60_000,
    );
    const gate2 = await host.invoke("answer_gate", { task_id: DRILL_TASK, gate: "gate_modeling", decision: "pass" });
    step(
      "⑥ 崩溃后无丢任务（补投题面后 modeling 延续完成）",
      modelingDone && gate2?.action === "next_stage",
      `progress≥1=${modelingDone}；gate=${JSON.stringify(gate2)}`,
    );

    // ⑦ 退出链（stop → 无遗留进程；协议硬上限 300ms）
    // 注：Windows 下 child.kill("SIGTERM") 为硬终止（不可捕获），检查点依赖逐阶段落盘；
    // POSIX 走引擎 SIGTERM 协作停机（engine/tests/test_entry_wiring.py 守护）——口径已登记风险项。
    const pidStop = host.childPid;
    const tStop = Date.now();
    host.stop();
    const exited = await waitUntil(() => host.childPid === null, 5_000);
    const stopMs = Date.now() - tStop;
    step(
      "⑦ 退出链（stop → 无遗留进程，≤300ms）",
      exited && stopMs <= 300,
      `pid=${pidStop}；退出耗时 ${stopMs}ms（协议硬上限 300ms）`,
    );
  } catch (err) {
    step("drill 执行异常", false, err instanceof Error ? err.message : String(err));
  } finally {
    if (host.childPid !== null) {
      try {
        host.stop();
      } catch {
        /* 清理失败不掩盖主错误 */
      }
      await waitUntil(() => host.childPid === null, 5_000);
    }
  }

  // ⑧ 检查点与留痕完好（node:sqlite 读 checkpoints.db + 客户端读取器读 audit.db）
  try {
    const mine = readRecentTasks(join(home, "audit.db"), 10).find((task) => task.taskId === DRILL_TASK);
    const db = new DatabaseSync(join(home, "checkpoints.db"), { readOnly: true });
    let checkpointStages = [];
    try {
      checkpointStages = db
        .prepare("SELECT stage, status FROM checkpoints WHERE task_id = ? ORDER BY stage")
        .all(DRILL_TASK)
        .map((row) => `${String(row["stage"])}:${String(row["status"])}`);
    } finally {
      db.close();
    }
    const noLoss = mine !== undefined && mine.eventCount >= trailBeforeCrash && trailBeforeCrash > 0;
    const checkpointsOk = checkpointStages.length >= 2 && checkpointStages.some((item) => item.startsWith("analysis:"));
    step(
      "⑧ 检查点与留痕完好（checkpoints.db 阶段行 + audit.db 崩溃前后无丢失）",
      noLoss && checkpointsOk,
      `留痕 ${mine?.eventCount ?? 0} 条（崩溃前 ${trailBeforeCrash}）；检查点 [${checkpointStages.join(", ")}]`,
    );
  } catch (err) {
    step("⑧ 检查点与留痕完好", false, err instanceof Error ? err.message : String(err));
  }
}

// ---- ⑨ 单实例（同一 userData 拒绝 + 不同 userData 控制组） ------------------
async function runSingletonSection() {
  const electronPath = require("electron");
  const userDataDir = join(home, "userdata");
  const env = { ...process.env, ERDOS_CHANNEL: "dev", ERDOS_USER_DATA_DIR: userDataDir };
  const launch = () => {
    const proc = spawn(electronPath, [clientRoot], { env, stdio: ["ignore", "pipe", "pipe"] });
    let log = "";
    proc.stdout.on("data", (chunk) => (log += chunk));
    proc.stderr.on("data", (chunk) => (log += chunk));
    return { proc, logOf: () => log };
  };

  const first = launch();
  let second = null;
  let control = null;
  try {
    // 就绪信号：首实例打印 [client] 启动日志（而非固定等待）
    const firstBooted = await waitUntil(() => first.logOf().includes("[client]"), 15_000);
    const firstAliveBeforeSecond = first.proc.exitCode === null;

    const tSecond = Date.now();
    second = launch();
    const secondCode = await new Promise((resolve) => {
      const timer = setTimeout(() => resolve("timeout"), 10_000);
      second.proc.on("exit", (code) => {
        clearTimeout(timer);
        resolve(code);
      });
    });
    const secondMs = Date.now() - tSecond;
    await sleep(800); // 给首实例处理 second-instance（聚焦）留出时间
    const firstAlive = first.proc.exitCode === null;
    // 锁拒绝证据：第二实例快速退出且未进入 boot（无 [client] 启动日志）
    const secondRejected = secondCode === 0 && secondMs <= 10_000 && !second.logOf().includes("[client]");

    // 控制组：不同 userData → 应正常 boot 且存活（排除「任何实例都秒退」的伪证）
    const controlEnv = { ...process.env, ERDOS_CHANNEL: "dev", ERDOS_USER_DATA_DIR: join(home, "userdata-control") };
    control = spawn(electronPath, [clientRoot], { env: controlEnv, stdio: ["ignore", "pipe", "pipe"] });
    let controlLog = "";
    control.stdout.on("data", (chunk) => (controlLog += chunk));
    control.stderr.on("data", (chunk) => (controlLog += chunk));
    const controlBooted = await waitUntil(() => controlLog.includes("[client]") || control.exitCode !== null, 15_000);
    await sleep(1_000);
    const controlAlive = control.exitCode === null;

    step(
      "⑨ 单实例（同 userData 锁拒绝 + 不同 userData 控制组存活）",
      firstBooted && firstAliveBeforeSecond && firstAlive && secondRejected && controlBooted && controlAlive,
      `第二实例 exit=${secondCode}（${secondMs}ms，未 boot=${!second.logOf().includes("[client]")}）；` +
        `首实例存活=${firstAliveBeforeSecond}→${firstAlive}；控制组 boot=${controlBooted} 存活=${controlAlive}`,
    );
  } catch (err) {
    step("⑨ 单实例", false, err instanceof Error ? err.message : String(err));
  } finally {
    killTree(control);
    killTree(second);
    killTree(first);
    await sleep(500);
  }
}

// ---- 主流程 ---------------------------------------------------------------
console.log(`[drill] W6 联调预演开始（引擎 home=${home}）`);
runContractCheck();
if (skipEngine) {
  step("①~⑧ 引擎链路", true, "按参数跳过（仅验证单实例）", true);
} else {
  await runEngineSection();
}
if (skipSingleton) {
  step("⑨ 单实例", true, "按参数跳过（10-24 人工或构建后重跑）", true);
} else if (!canRunSingleton) {
  step("⑨ 单实例", true, "未构建 dist（先 npm run build 后重跑）", true);
} else {
  await runSingletonSection();
}

// ---- 证据落盘（写盘失败 fail-closed：不得「绿而无证据」） --------------------
const eventsByKind = {};
for (const event of events) eventsByKind[event.event] = (eventsByKind[event.event] ?? 0) + 1;
let engineVersion = "unknown";
try {
  engineVersion = execSync(`"${enginePython}" -m engine --version`, { cwd: repoRoot, encoding: "utf8" }).trim();
} catch {
  /* 版本获取失败不阻断主链（证据中标注 unknown） */
}
let gitRev = "unknown";
try {
  gitRev = execSync("git rev-parse --short HEAD", { cwd: repoRoot, encoding: "utf8" }).trim();
} catch {
  /* 非 git 环境 */
}
const executed = steps.filter((item) => !item.skipped);
const evidence = {
  drill: "W6-J1024",
  ranAt: new Date().toISOString(),
  ok: executed.every((item) => item.ok),
  home: basename(home), // 脱敏：完整路径见控制台
  platform: process.platform,
  engine: basename(enginePython ?? "unknown"),
  engineVersion,
  gitRev,
  passed: executed.filter((item) => item.ok).length,
  executed: executed.length,
  skipped: steps.length - executed.length,
  protocolErrors,
  steps,
  states,
  eventCount: events.length,
  eventsByKind,
};
try {
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(evidence, null, 2), "utf-8");
  console.log(`[drill] 证据 → ${outPath}`);
} catch (err) {
  step("证据落盘", false, err instanceof Error ? err.message : String(err));
  evidence.ok = false;
}
console.log(
  `[drill] 结果：${evidence.ok ? "通过" : "失败"}（${evidence.passed}/${evidence.executed} 步` +
    `${evidence.skipped > 0 ? `，${evidence.skipped} 跳过` : ""}）`,
);
process.exit(evidence.ok ? 0 : 1);