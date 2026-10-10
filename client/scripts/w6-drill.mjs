/**
 * W6 · J-1024 三进程汇合联调预演（10-24 硬门前置，可无人值守重复执行）。
 *
 * 覆盖联调清单（对齐手册 W6 步骤①②；Key/厂商端点通道均已脚本化，10-24 现场复跑）：
 *   ⓪ 契约清单核对（方法：schema ↔ client ↔ engine 三方；事件：schema ↔ client，引擎侧由
 *     engine/tests/test_contract_alignment.py 守护——本批已实测 4 例通过）
 *   ① 引擎启动与握手（spawn → initialize → ready，EC-T6 ≤10s）
 *   ② task_create 登记题面
 *   ③ 事件分流（analysis 完成；RPC 层未知事件零计数——未知事件在 rpc.ts 拒绝并计入协议错误）
 *   ④ 门禁 gate_analysis 通过（answer_gate）
 *   ⑤ kill 看门狗（强杀引擎 → crashed → 自动重启 → ready；重启次数由状态轨迹证实）
 *   ⑥ 崩溃后无丢任务（重启后直接续跑 modeling；题面随检查点库落盘并自动水合）
 *   ⑦ 退出链（stop → 无遗留进程，协议硬上限 300ms；Windows SIGTERM=硬终止语义见风险登记）
 *   ⑧ 检查点与留痕完好（node:sqlite 读 checkpoints.db 行 + 客户端读取器读 audit.db；
 *     崩溃前留痕不得减少）
 *   ⑨ 单实例（同一 userData：第二实例被锁拒绝且未进入 boot；控制组：不同 userData 可正常启动）
 *   ⑩ 真实 Key 通道（--with-key；mock 厂商端点 200/401）：验证「首行注入 → Authorization → /models 探测」
 *      （10-24 待签认项；不判定 Key 无效：红线口径）
 *   ⑪ 厂商端点通道（--with-vendor；外部真实/模拟厂商端点，10-24 现场复跑同一断言）：环境变量
 *      LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_ID（必填）、LLM_PROVIDER（选填，默认 dashscope-compat）；
 *      密钥仅经环境变量注入与首行传递——不落盘、不入证据（证据仅存脱敏 keyMasked）
 *   --with-solving（可选）：solving 阶段专项（真实沙箱执行，实测约 60~120s；10-24 待签认项）
 *
 * 用法（client/ 目录）：
 *   node scripts/w6-drill.mjs [--skip-singleton] [--skip-engine] [--with-solving] [--with-key] [--with-vendor] [--out <evidence.json>]
 * 前置：engine/.venv（或 ERDOS_ENGINE_PYTHON 覆盖）；⑨ 需已构建 dist（npm run build）。
 * 退出码：0=全项通过；1=存在失败步骤；2=前置缺失。
 * 证据：JSON 默认写 reports/w6-drill-<日期>.json（steps/状态轨迹/事件统计/版本/计时；
 * 路径已脱敏为 basename，完整 home 见控制台输出）。
 */
import { execSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";

import { EngineHost } from "../main/engine-host/host.ts";
import { readRecentTasks } from "../main/engine-trail.ts";
import { maskSecret, maskText } from "../main/secret-masker.ts";
import { ENGINE_EVENT_NAMES, RPC_METHODS } from "../shared/ipc.ts";

const require = createRequire(import.meta.url);
const clientRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(clientRoot, "..");

// ---- 参数与前置 -----------------------------------------------------------
const argv = process.argv.slice(2);
const skipSingleton = argv.includes("--skip-singleton");
const skipEngine = argv.includes("--skip-engine");
const withSolving = argv.includes("--with-solving");
const withKey = argv.includes("--with-key");
const withVendor = argv.includes("--with-vendor");
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
/** 沙箱镜像摘要等告警行（10-24 待签认项参考；不失败仅记录）。 */
const sandboxWarnings = [];
/** solving 专项耗时（--with-solving）。 */
let solvingMs = null;
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
    onLog: (line) => {
      if (/摘要|digest/i.test(line)) sandboxWarnings.push(line);
      console.log(`[engine] ${line}`);
    },
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

    // ⑥ 崩溃后无丢任务（重启后直接续跑 modeling；题面随检查点库落盘并自动水合——R1 已关闭）
    await host.invoke("start_stage", { task_id: DRILL_TASK, stage: "modeling" });
    const modelingDone = await waitUntil(
      () => events.some((e) => e.event === "stage.progress" && e.stage === "modeling" && e.progress >= 1),
      60_000,
    );
    const gate2 = await host.invoke("answer_gate", { task_id: DRILL_TASK, gate: "gate_modeling", decision: "pass" });
    // 题面持久化数据侧核对（客户端直读引擎库）：原始题面随 task_create 落盘、重启后可用于水合
    const taskInputDb = new DatabaseSync(join(home, "checkpoints.db"), { readOnly: true });
    let persisted = null;
    try {
      persisted = taskInputDb.prepare("SELECT title, problem_text FROM task_inputs WHERE task_id = ?").get(DRILL_TASK);
    } finally {
      taskInputDb.close();
    }
    const taskInputOk =
      persisted?.title === "W6 联调预演" && String(persisted?.problem_text ?? "").includes("计算 1+1");
    step(
      "⑥ 崩溃后无丢任务（重启后直接续跑 modeling；题面落盘并水合）",
      modelingDone && gate2?.action === "next_stage" && taskInputOk,
      `progress≥1=${modelingDone}；gate=${JSON.stringify(gate2)}；题面落盘=${taskInputOk}（title=${persisted?.title ?? "-"}）`,
    );

    // ⑥b solving 专项（--with-solving；真实沙箱执行，10-24 待签认口径；冷启动含镜像拉取可能显著变慢）
    if (withSolving) {
      const tSolve = Date.now();
      await host.invoke("start_stage", { task_id: DRILL_TASK, stage: "solving" });
      const solvingDone = await waitUntil(
        () => events.some((e) => e.event === "stage.progress" && e.stage === "solving" && e.progress >= 1),
        240_000,
      );
      solvingMs = Date.now() - tSolve;
      const toolEvents = events.filter((e) => e.event === "tool.call" || e.event === "tool.result").length;
      // 数据侧证据：solving 检查点行已落盘（非「假执行」的最低证据；沙箱模式见 sandboxWarnings）
      const solvingDb = new DatabaseSync(join(home, "checkpoints.db"), { readOnly: true });
      let solvingStatus = "missing";
      try {
        const row = solvingDb
          .prepare("SELECT status FROM checkpoints WHERE task_id = ? AND stage = 'solving'")
          .get(DRILL_TASK);
        if (row !== undefined) solvingStatus = String(row["status"]);
      } finally {
        solvingDb.close();
      }
      step(
        "⑥b solving 专项（真实沙箱执行完成）",
        solvingDone && solvingStatus !== "missing",
        `耗时 ${solvingMs}ms；工具事件 ${toolEvents} 条；检查点 solving:${solvingStatus}`,
      );
    } else {
      step("⑥b solving 专项", true, "按参数跳过（--with-solving 启用；10-24 待签认项）", true);
    }

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

// ---- ⑩ 真实 Key 通道（--with-key；mock 厂商端点，验证首行注入→鉴权→探测链路） --
async function runKeyChannelSection() {
  /** mock 收到的请求（url + Authorization 头）：证「首行注入 → 鉴权头到端」。 */
  const received = [];
  let server = null;
  let host = null;
  let okResult = null;
  let badResult = null;
  const keyHome = mkdtempSync(join(tmpdir(), "erdos-w6-key-"));
  // Key 模式装配要求（引擎 __main__：ERDOS_MODEL_BASE_URL/NAME）；finally 恢复现场
  const prevBaseUrl = process.env.ERDOS_MODEL_BASE_URL;
  const prevModelName = process.env.ERDOS_MODEL_NAME;
  try {
    server = createServer((req, res) => {
      received.push({ url: req.url ?? "", auth: String(req.headers["authorization"] ?? "") });
      if ((req.url ?? "").startsWith("/bad/")) {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message: "invalid api key（mock：401 仅表示探测未通过）" } }));
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ object: "list", data: [{ id: "drill-mock-model", object: "model" }] }));
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const port = server.address().port;
    const okBase = `http://127.0.0.1:${port}/ok`;
    const badBase = `http://127.0.0.1:${port}/bad`;
    process.env.ERDOS_MODEL_BASE_URL = okBase;
    process.env.ERDOS_MODEL_NAME = "drill-mock-model";

    host = new EngineHost({
      command: enginePython,
      args: ["-m", "engine"],
      cwd: repoRoot,
      home: keyHome,
      key: () => "sk-drill-mock", // 首行一次性注入（mock 凭据；仅演练用，勿用于真实厂商）
      onEvent: () => {},
      onStateChange: () => {},
      onLog: () => {},
      onProtocolError: (err) => console.warn(`[drill] 协议错误：${err.message}`),
    });
    await host.reloadKey(); // spawn（首行注入）→ initialize → ready
    okResult = await host.invoke("provider_test", { base_url: okBase, model: "drill-mock-model", provider: "openai" });
    badResult = await host.invoke("provider_test", { base_url: badBase, model: "drill-mock-model", provider: "openai" });
    const mockKey = "Bearer sk-drill-mock";
    const okAuth = received.some((item) => item.url.startsWith("/ok/models") && item.auth === mockKey);
    const badAuth = received.some((item) => item.url.startsWith("/bad/models") && item.auth === mockKey);
    step(
      "⑩ 真实 Key 通道（mock 厂商端点：首行注入 → Authorization → /models 探测）",
      okResult?.ok === true && badResult?.ok === false && okAuth && badAuth && received.length >= 2,
      `200→ok=${okResult?.ok}（tool_mode=${okResult?.tool_mode}）；401→ok=${badResult?.ok}` +
        `（不判定 Key 无效：红线口径）；鉴权头到端 ok/bad=${okAuth}/${badAuth}；mock 请求 ${received.length} 次`,
    );
  } catch (err) {
    step("⑩ 真实 Key 通道", false, err instanceof Error ? err.message : String(err));
  } finally {
    if (host !== null && host.childPid !== null) host.stop();
    if (host !== null) await waitUntil(() => host.childPid === null, 5_000);
    if (server !== null) server.close();
    rmSync(keyHome, { recursive: true, force: true });
    if (prevBaseUrl === undefined) delete process.env.ERDOS_MODEL_BASE_URL;
    else process.env.ERDOS_MODEL_BASE_URL = prevBaseUrl;
    if (prevModelName === undefined) delete process.env.ERDOS_MODEL_NAME;
    else process.env.ERDOS_MODEL_NAME = prevModelName;
  }
  const mockKey = "Bearer sk-drill-mock";
  return {
    ok: okResult?.ok ?? null,
    toolMode: okResult?.tool_mode ?? null,
    bad: badResult?.ok ?? null,
    authSeen: received.some((item) => item.url.startsWith("/ok/models") && item.auth === mockKey),
    requests: received.length,
  };
}

// ---- ⑪ 厂商端点通道（--with-vendor；外部真实/模拟厂商端点，10-24 现场复跑口径） --
/**
 * 从环境变量读取外部厂商配置（密钥不落盘、不入证据、不进日志——仅首行注入驻内存）：
 *   LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_ID（必填）· LLM_PROVIDER（选填，默认 dashscope-compat）
 *   LLM_TIMEOUT 仅作用于模型调用，不参与 /models 探测（探测超时为引擎侧固定 2s），本段不读取。
 *
 * 断言与本 drill ⑩（mock）同口径：provider_test ok===true；真实端点不可控，故不含 401 对照分支。
 * 探测未通过如实记录（端点不可达/未授权/超时）——不判定 Key 无效（红线口径）。
 */
async function runVendorChannelSection() {
  const baseUrl = (process.env.LLM_BASE_URL ?? "").trim();
  const apiKey = (process.env.LLM_API_KEY ?? "").trim();
  const model = (process.env.LLM_MODEL_ID ?? "").trim();
  const provider = (process.env.LLM_PROVIDER ?? "dashscope-compat").trim();
  /** 出口文本统一脱敏：maskText 词表 + 逐字替换本 Key（覆盖词表不匹配的自定义格式）。 */
  const scrub = (text) => {
    let result = maskText(String(text ?? ""));
    if (apiKey !== "") result = result.replaceAll(apiKey, maskSecret(apiKey));
    return result;
  };
  if (baseUrl === "" || apiKey === "" || model === "") {
    step(
      "⑪ 厂商端点通道（真实/模拟厂商复跑）",
      false,
      "缺配置：需 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_ID 环境变量（密钥仅经环境注入，不入证据）",
    );
    return null;
  }
  let hostName = "unknown";
  try {
    hostName = new URL(baseUrl).host; // 证据仅记 host（不落完整 URL）
  } catch {
    /* 非法 URL：探测侧会失败，host 记 unknown */
  }
  const vendorHome = mkdtempSync(join(tmpdir(), "erdos-w6-vendor-"));
  // 引擎 Key 模式装配要求（引擎 __main__：ERDOS_MODEL_BASE_URL/NAME）；finally 恢复现场
  const prevBaseUrl = process.env.ERDOS_MODEL_BASE_URL;
  const prevModelName = process.env.ERDOS_MODEL_NAME;
  let host = null;
  let result = null;
  try {
    process.env.ERDOS_MODEL_BASE_URL = baseUrl;
    process.env.ERDOS_MODEL_NAME = model;
    host = new EngineHost({
      command: enginePython,
      args: ["-m", "engine"],
      cwd: repoRoot,
      home: vendorHome,
      key: () => apiKey, // 首行一次性注入（明文仅驻内存；不落日志/证据）
      onEvent: () => {},
      onStateChange: () => {},
      onLog: () => {}, // 引擎日志可能含上游回显——一律丢弃，杜绝明文外泄
      onProtocolError: (err) => console.warn(`[drill] 协议错误：${scrub(err.message)}`),
    });
    await host.reloadKey(); // spawn（首行注入）→ initialize → ready
    result = await host.invoke("provider_test", { base_url: baseUrl, model, provider });
    const ok = result?.ok === true;
    step(
      "⑪ 厂商端点通道（真实/模拟厂商复跑：首行注入 → Authorization → /models 探测）",
      ok,
      `host=${hostName}；model=${model}；provider=${provider}；ok=${result?.ok}` +
        `（models_endpoint=${result?.models_endpoint}，tool_mode=${result?.tool_mode}）` +
        (ok ? "" : "；探测未通过（端点不可达/未授权/超时）——不判定 Key 无效：红线口径") +
        `；key=${maskSecret(apiKey)}`,
    );
  } catch (err) {
    step("⑪ 厂商端点通道", false, scrub(err instanceof Error ? err.message : String(err)));
  } finally {
    if (host !== null && host.childPid !== null) host.stop();
    if (host !== null) await waitUntil(() => host.childPid === null, 5_000);
    rmSync(vendorHome, { recursive: true, force: true });
    if (prevBaseUrl === undefined) delete process.env.ERDOS_MODEL_BASE_URL;
    else process.env.ERDOS_MODEL_BASE_URL = prevBaseUrl;
    if (prevModelName === undefined) delete process.env.ERDOS_MODEL_NAME;
    else process.env.ERDOS_MODEL_NAME = prevModelName;
  }
  return {
    host: hostName,
    model,
    provider,
    ok: result?.ok ?? null,
    modelsEndpoint: result?.models_endpoint ?? null,
    toolMode: result?.tool_mode ?? null,
    keyMasked: maskSecret(apiKey), // 证据仅存脱敏形式
  };
}

// ---- 主流程 ---------------------------------------------------------------
console.log(`[drill] W6 联调预演开始（引擎 home=${home}）`);
runContractCheck();
if (skipEngine) {
  step("①~⑧ 引擎链路", true, "按参数跳过（仅验证单实例）", true);
} else {
  await runEngineSection();
}
let keyChannelEvidence = null;
if (!withKey) {
  step("⑩ 真实 Key 通道", true, "按参数跳过（--with-key 启用；10-24 待签认项）", true);
} else if (enginePython === null) {
  step("⑩ 真实 Key 通道", true, "跳过：未找到引擎环境", true);
} else {
  try {
    keyChannelEvidence = await runKeyChannelSection();
  } catch (err) {
    // 兜底：段内未捕获异常不得中断主流程与证据落盘
    step("⑩ 真实 Key 通道", false, err instanceof Error ? err.message : String(err));
  }
}
let vendorChannelEvidence = null;
if (!withVendor) {
  step("⑪ 厂商端点通道", true, "按参数跳过（--with-vendor 启用；10-24 现场复跑项）", true);
} else if (enginePython === null) {
  step("⑪ 厂商端点通道", true, "跳过：未找到引擎环境", true);
} else {
  try {
    vendorChannelEvidence = await runVendorChannelSection();
  } catch (err) {
    // 兜底：段内未捕获异常不得中断主流程与证据落盘（消息统一脱敏）
    step("⑪ 厂商端点通道", false, maskText(err instanceof Error ? err.message : String(err)));
  }
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
  solvingMs,
  sandboxWarnings,
  keyChannel: keyChannelEvidence,
  vendorChannel: vendorChannelEvidence,
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