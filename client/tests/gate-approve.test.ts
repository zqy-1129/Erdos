/**
 * 门禁审批纯函数单测（FE-APPROVE W13）：重试计数、状态快照收敛、冲突识别。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  applyBatch,
  applyStatusSnapshot,
  initialEngineState,
  isGateConflict,
} from "../renderer/src/state/engine-slice.ts";
import type { EngineEvent, GetStatusResult } from "../shared/ipc.ts";

function gateFailed(gate: string, step = 0): EngineEvent {
  return {
    trace_id: `t-${step}`,
    event: "gate.failed",
    task_id: "t1",
    gate,
    reason: "原因",
    timestamp: new Date(10_000 + step).toISOString(),
  };
}

describe("门禁重试计数（W13）", () => {
  it("gate.failed 计数 gateRetries", () => {
    const state = applyBatch(initialEngineState, [gateFailed("modeling")]);
    assert.equal(state.gateRetries["modeling"], 1);
  });

  it("同一门禁多次失败累计；不同门禁各自计数", () => {
    let state = applyBatch(initialEngineState, [gateFailed("modeling")]);
    state = applyBatch(state, [gateFailed("modeling", 1)]);
    state = applyBatch(state, [gateFailed("solving", 2)]);
    assert.equal(state.gateRetries["modeling"], 2);
    assert.equal(state.gateRetries["solving"], 1);
  });
});

describe("applyStatusSnapshot 状态收敛（W13）", () => {
  it("同步 running/stage/taskId", () => {
    const status: GetStatusResult = {
      engine: "running",
      task: { task_id: "t1", stage: "solving", status: "running" },
    };
    const state = applyStatusSnapshot(initialEngineState, status);
    assert.equal(state.running, true);
    assert.equal(state.stage, "solving");
    assert.equal(state.taskId, "t1");
  });

  it("无 task 时保留原 taskId/stage，running 归一", () => {
    const base = { ...initialEngineState, taskId: "t0", stage: "analysis" as const };
    const state = applyStatusSnapshot(base, { engine: "idle", task: null });
    assert.equal(state.taskId, "t0");
    assert.equal(state.stage, "analysis");
    assert.equal(state.running, false);
  });

  it("task 为空时用 orchestrator.current_stage 兜底", () => {
    const status: GetStatusResult = {
      engine: "paused",
      task: null,
      orchestrator: {
        task_id: "t1",
        current_stage: "modeling",
        stage_index: 1,
        stages: {},
        gate_decision: null,
      },
    };
    const state = applyStatusSnapshot(initialEngineState, status);
    assert.equal(state.stage, "modeling");
    assert.equal(state.taskId, "t1");
    assert.equal(state.running, false);
  });
});

describe("isGateConflict 冲突识别（W13）", () => {
  it("识别引擎门禁冲突错误（RPC 收敛后的 message）", () => {
    assert.equal(isGateConflict(new Error("引擎错误 -32602：门禁冲突：无挂起门禁")), true);
  });

  it("非冲突错误返回 false", () => {
    assert.equal(isGateConflict(new Error("网络超时")), false);
    assert.equal(isGateConflict("不是错误对象"), false);
  });
});
