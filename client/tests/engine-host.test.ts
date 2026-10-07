/**
 * EngineHost 状态机集成测试（FE-HOST W3 验收）。
 *
 * 用真实引擎进程（engine/.venv 的 python + FakeLLM 无 Key 模式）验证：
 * idle → spawning → ready（initialize 握手）、get_status、start_stage → running、
 * stop → stopped 优雅退出、看门狗重启。不依赖真实 Key / 网络。
 *
 * 需要 engine 依赖已装好（engine/.venv）；cwd 指向 engine 包父目录（仓库根）。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { exec } from "node:child_process";

import { EngineHost, type EngineHostState } from "../main/engine-host/host.ts";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** 解析引擎可执行（跨平台）：环境变量优先，其次探测 engine/.venv；无则返回 null（测试 skip）。 */
function resolveEnginePython(): string | null {
  if (process.env.ERDOS_ENGINE_PYTHON) return process.env.ERDOS_ENGINE_PYTHON;
  const candidates = [
    join(repoRoot, "engine", ".venv", "Scripts", "python.exe"), // Windows
    join(repoRoot, "engine", ".venv", "bin", "python"), // POSIX
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

const enginePython = resolveEnginePython();

async function waitFor(fn: () => boolean, timeoutMs: number, label: string): Promise<void> {
  const start = Date.now();
  while (!fn()) {
    if (Date.now() - start > timeoutMs) throw new Error(`waitFor 超时：${label}`);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

function makeHost(): { host: EngineHost; stateLog: EngineHostState[]; events: unknown[]; logs: string[] } {
  const stateLog: EngineHostState[] = [];
  const events: unknown[] = [];
  const logs: string[] = [];
  const host = new EngineHost({
    command: enginePython!, // describe 已在无引擎时 skip，此处非空
    args: ["-m", "engine"],
    cwd: repoRoot,
    home: mkdtempSync(join(tmpdir(), "erdos-host-")),
    key: () => null, // 无 Key 模式（FakeLLM）
    onEvent: (event) => events.push(event),
    onStateChange: (state) => stateLog.push(state),
    onLog: (line) => logs.push(line), // 引擎 stderr + 看门狗日志
    onProtocolError: () => {},
  });
  return { host, stateLog, events, logs };
}

describe("EngineHost 状态机（真实引擎 + FakeLLM）", { skip: enginePython === null }, () => {
  it("懒启动：idle → spawning → ready（initialize 握手成功）", async () => {
    const { host, stateLog } = makeHost();
    try {
      assert.equal(host.currentState, "idle");
      host.ensureStarted();
      assert.equal(host.currentState, "spawning");
      await waitFor(() => host.currentState === "ready", 20_000, "spawning→ready");
      assert.ok(stateLog.includes("ready"), `状态日志应含 ready：${JSON.stringify(stateLog)}`);
    } finally {
      host.stop();
    }
  });

  it("ready 后 invoke get_status 返回引擎状态快照（engine 字段存在）", async () => {
    const { host } = makeHost();
    try {
      const status = await host.invoke<Record<string, unknown>>("get_status", {});
      assert.ok(status && typeof status === "object", "get_status 应返回对象");
      assert.ok("engine" in status, `快照应含 engine 字段：${JSON.stringify(status)}`);
    } finally {
      host.stop();
    }
  });

  it("invoke start_stage 受理并转入 running（真实三进程事件回流）", async () => {
    const { host, events, stateLog } = makeHost();
    try {
      await host.invoke("task_create", { task_id: "t1", title: "样例", problem_text: "题面" });
      const accepted = await host.invoke<{ task_id: string; stage: string }>("start_stage", {
        task_id: "t1",
        stage: "analysis",
      });
      assert.equal(accepted.stage, "analysis");
      await waitFor(() => events.some((e) => (e as { event?: string }).event === "stage.progress"), 30_000, "stage.progress 事件回流");
      assert.ok(stateLog.includes("running"), `应经历 running：${JSON.stringify(stateLog)}`);
    } finally {
      host.stop();
    }
  });

  it("stop 优雅退出 → stopped（无遗留进程语义）", async () => {
    const { host } = makeHost();
    try {
      host.ensureStarted();
      await waitFor(() => host.currentState === "ready", 20_000, "就绪");
      host.stop();
      assert.equal(host.currentState, "stopped");
      await waitFor(() => host.childPid === null, 5000, "引擎进程退出后清空引用");
      assert.equal(host.childPid, null);
    } finally {
      host.stop();
    }
  });

  it("引擎进程被 kill → 看门狗自动重启（crashed → spawning → ready）", async () => {
    const { host, stateLog, logs } = makeHost();
    try {
      host.ensureStarted();
      await waitFor(() => host.currentState === "ready", 20_000, "初次就绪");
      const pid = host.childPid;
      assert.ok(pid != null, "就绪后应有引擎 pid");

      await new Promise<void>((resolve) => exec(`taskkill /PID ${pid} /T /F`, () => resolve()));

      await waitFor(() => host.currentState === "ready" && host.childPid !== null && host.childPid !== pid, 25_000, "看门狗重启后就绪（新 pid）");
      assert.ok(stateLog.includes("crashed"), `应经历 crashed：${JSON.stringify(stateLog)}`);
      assert.ok(logs.some((l) => l.includes("看门狗")), `应记录看门狗重启日志：${JSON.stringify(logs)}`);
    } finally {
      host.stop();
    }
  });
});
