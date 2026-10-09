/**
 * 壳级冒烟（FE-HOST W3 验收预演 / 10-24 J-1024 前置）：ERDOS_SMOKE=1 时由主进程入口调用。
 *
 * 覆盖链路（不含渲染层 UI，无窗口，可无人值守重复执行）：
 *   spawn 引擎（首行 Key 注入）→ initialize 握手 → task_create 登记题面 →
 *   四阶段顺序推进 [analysis→modeling→solving→writing]：
 *     start_stage → 等待 stage.progress≥1 → answer_gate(pass)（门禁 gate_<stage>）→
 *   artifact.ready(paper) → 优雅退出（无遗留进程）。
 *
 * 驱动序列对齐 scripts/run_paper_e2e.py（SP1-7 验收同一实现 RpcTaskFlow）与引擎
 * graph.py 语义（每阶段后门禁 interrupt，pass 后 stage_index+1）。
 *
 * 产物：证据 JSON 写入 ERDOS_SMOKE_OUT（steps/事件统计/状态轨迹/耗时）；
 * 返回值 ok=全部步骤通过；退出码由入口按 ok 取 0/1。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { EngineHost, type EngineHostState } from "./engine-host/host.ts";
import type { EngineEvent, StageName } from "../shared/ipc.ts";

/** 冒烟任务 ID（每次执行使用独立 home，避免检查点续跑干扰）。 */
const SMOKE_TASK_ID = "smoke-task";
/** 四阶段固定顺序（禁跳级，与引擎 STAGES 一致）。 */
const STAGES: StageName[] = ["analysis", "modeling", "solving", "writing"];

export interface SmokeStep {
  name: string;
  ok: boolean;
  detail: string;
  /** 相对上一步的耗时（ms），便于定位慢环节）。 */
  ms: number;
}

export interface SmokeEvidence {
  ok: boolean;
  startedAt: string;
  finishedAt: string;
  /** 引擎数据根目录（留痕/检查点/产物）。 */
  home: string;
  steps: SmokeStep[];
  eventCount: number;
  eventsByKind: Record<string, number>;
  /** 事件时间线（不含 model.delta，最多 200 条；供失败归因与耗时分析）。 */
  eventLog: Array<{ kind: string; stage?: string; progress?: number; at: string }>;
  /** EngineHost 状态转移轨迹（idle→spawning→ready→running→…→stopped）。 */
  states: EngineHostState[];
  error: string | null;
}

export interface SmokeOptions {
  command: string;
  args: string[];
  cwd?: string;
  /** 引擎数据根目录（ERDOS_ENGINE_HOME）。 */
  home: string;
  /** 证据 JSON 输出路径。 */
  outPath: string;
  /** 首行注入的 Key（null = 无 Key FakeLLM 模式）。 */
  key?: string | null;
  /** 单阶段等待完成事件的超时（默认 180s；FakeLLM 下 solving 走真实沙箱执行，较慢）。 */
  stageTimeoutMs?: number;
  onLog?: (line: string) => void;
}

/** 等待条件成立（轮询 25ms）；超时返回 false。 */
async function waitUntil(condition: () => boolean, timeoutMs: number): Promise<boolean> {
  const start = Date.now();
  while (!condition()) {
    if (Date.now() - start > timeoutMs) return false;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return true;
}

/** 执行壳级冒烟并落盘证据（失败不抛错，以 evidence.ok=false 表达）。 */
export async function runSmoke(options: SmokeOptions): Promise<SmokeEvidence> {
  const startedAt = new Date().toISOString();
  const stageTimeout = options.stageTimeoutMs ?? 180_000;
  const steps: SmokeStep[] = [];
  const events: EngineEvent[] = [];
  const states: EngineHostState[] = [];
  let mark = Date.now();
  const step = (name: string, ok: boolean, detail = ""): void => {
    steps.push({ name, ok, detail, ms: Date.now() - mark });
    mark = Date.now();
  };

  const host = new EngineHost({
    command: options.command,
    args: options.args,
    cwd: options.cwd,
    home: options.home,
    key: () => options.key ?? null,
    onEvent: (event) => {
      if (events.length < 2000) events.push(event);
    },
    onStateChange: (state) => states.push(state),
    onLog: (line) => options.onLog?.(line),
    onProtocolError: (err) => options.onLog?.(`[smoke] 协议错误：${err.message}`),
  });

  let error: string | null = null;
  try {
    // 1) 启动 + 握手（复用 FE-KEYIN 的 reloadKey：idle → spawn（首行注入）→ ready）
    await host.reloadKey();
    step("引擎启动与握手（initialize/get_status）", host.currentState === "ready", `state=${host.currentState}`);

    // 2) 题面登记（EN-PAPER：start_stage 前必须调用）
    const created = await host.invoke<{ task_id: string; status: string }>("task_create", {
      task_id: SMOKE_TASK_ID,
      title: "壳级冒烟",
      problem_text: "冒烟题面：计算 1+1（FakeLLM 确定性假模型，不发起真实模型调用）",
    });
    step("task_create 登记题面", created.status === "created", JSON.stringify(created));

    // 3) 四阶段顺序推进 + 门禁自动通过（与 run_paper_e2e.py 同序）
    for (const stage of STAGES) {
      const before = events.length;
      const accepted = await host.invoke<{ stage: string }>("start_stage", { task_id: SMOKE_TASK_ID, stage });
      const progressed = await waitUntil(
        () => events.some((e) => e.event === "stage.progress" && e.stage === stage && e.progress >= 1),
        stageTimeout,
      );
      step(
        `阶段 ${stage}：受理 + 完成事件（progress≥1）`,
        accepted.stage === stage && progressed,
        `accepted=${accepted.stage}；新增事件 ${events.length - before}`,
      );

      const answer = await host.invoke<{ action?: string }>("answer_gate", {
        task_id: SMOKE_TASK_ID,
        gate: `gate_${stage}`,
        decision: "pass",
      });
      step(
        `门禁 gate_${stage} 通过（answer_gate）`,
        answer.action === "next_stage" || answer.action === "complete",
        JSON.stringify(answer),
      );
    }

    // 4) 论文产物事件（writing 阶段完成时随 paper_sha256 发出）
    const paperReady = await waitUntil(
      () => events.some((e) => e.event === "artifact.ready" && e.artifact === "paper"),
      15_000,
    );
    step("artifact.ready（paper）", paperReady, `引擎 home=${options.home}`);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
    step("冒烟执行异常", false, error);
  } finally {
    host.stop();
  }

  // 5) 优雅退出：无遗留引擎子进程（SIGTERM → 300ms → taskkill 兜底）
  await waitUntil(() => host.childPid === null, 5000);
  step("优雅退出（无遗留进程）", host.childPid === null, `pid=${host.childPid ?? "-"}`);

  const eventsByKind: Record<string, number> = {};
  for (const event of events) eventsByKind[event.event] = (eventsByKind[event.event] ?? 0) + 1;

  // 事件时间线（跳过 model.delta：高频且无诊断价值；上限 200 条防证据膨胀）
  const eventLog: SmokeEvidence["eventLog"] = [];
  for (const event of events) {
    if (event.event === "model.delta") continue;
    if (eventLog.length >= 200) break;
    const entry: SmokeEvidence["eventLog"][number] = { kind: event.event, at: event.timestamp };
    if (event.event === "stage.progress") {
      entry.stage = event.stage;
      entry.progress = event.progress;
    }
    eventLog.push(entry);
  }

  const evidence: SmokeEvidence = {
    ok: steps.every((s) => s.ok),
    startedAt,
    finishedAt: new Date().toISOString(),
    home: options.home,
    steps,
    eventCount: events.length,
    eventsByKind,
    eventLog,
    states,
    error,
  };

  try {
    mkdirSync(dirname(options.outPath), { recursive: true });
    writeFileSync(options.outPath, JSON.stringify(evidence, null, 2), "utf-8");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    options.onLog?.(`[smoke] 证据写入失败：${message}`);
  }
  return evidence;
}