/**
 * 工具事件归约单测（FE-TOOLUI W12 验收：engine-slice 纯函数 ≥8 例）。
 *
 * 覆盖：tool.call 建项、tool.result 按 callId 回填（成功/失败）、无匹配忽略、
 * 滑动窗口 500 上限、model.delta 追加与截断、混批归约、引用不变（局部更新语义）。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { applyBatch, initialEngineState } from "../renderer/src/state/engine-slice.ts";
import type { EngineEvent, StageName } from "../shared/ipc.ts";

function toolCall(callId: string, tool: string, stage: StageName, step = 0): EngineEvent {
  return {
    trace_id: `t-${step}`,
    event: "tool.call",
    task_id: "t1",
    stage,
    call_id: callId,
    tool,
    args_summary: "脱敏摘要",
    timestamp: new Date(10_000 + step).toISOString(),
  };
}

function toolResult(
  callId: string,
  tool: string,
  ok: boolean,
  step = 0,
  extra: { result_ref?: string; error?: string } = {},
): EngineEvent {
  return {
    trace_id: `t-${step}`,
    event: "tool.result",
    task_id: "t1",
    call_id: callId,
    tool,
    ok,
    duration_ms: 100,
    timestamp: new Date(20_000 + step).toISOString(),
    ...extra,
  };
}

function delta(text: string, step = 0): EngineEvent {
  return {
    trace_id: `t-${step}`,
    event: "model.delta",
    task_id: "t1",
    delta: text,
    timestamp: new Date(30_000 + step).toISOString(),
  };
}

function progress(stage: StageName, value: number): EngineEvent {
  return {
    trace_id: "tp",
    event: "stage.progress",
    task_id: "t1",
    stage,
    progress: value,
    timestamp: new Date(40_000).toISOString(),
  };
}

describe("工具事件归约（FE-TOOLUI W12）", () => {
  it("tool.call 建项：追加到对应 stage 时间线（ok=null 进行中）", () => {
    const patch = applyBatch(initialEngineState, [toolCall("c1", "execute_code", "solving")]);
    const items = patch.toolTimeline["solving"] ?? [];
    assert.equal(items.length, 1);
    assert.equal(items[0].callId, "c1");
    assert.equal(items[0].ok, null);
    assert.equal(patch.stage, "solving");
  });

  it("tool.result 按 callId 回填 ok/durationMs/resultRef", () => {
    let state = applyBatch(initialEngineState, [toolCall("c1", "execute_code", "solving")]);
    state = applyBatch(state, [toolResult("c1", "execute_code", true, 1, { result_ref: "out/r.json" })]);
    const item = state.toolTimeline["solving"]![0];
    assert.equal(item.ok, true);
    assert.equal(item.durationMs, 100);
    assert.equal(item.resultRef, "out/r.json");
  });

  it("tool.result 失败：ok=false 且携带 error", () => {
    let state = applyBatch(initialEngineState, [toolCall("c1", "plot_figure", "solving")]);
    state = applyBatch(state, [toolResult("c1", "plot_figure", false, 1, { error: "沙箱失败" })]);
    const item = state.toolTimeline["solving"]![0];
    assert.equal(item.ok, false);
    assert.equal(item.error, "沙箱失败");
  });

  it("tool.result 无匹配 callId 静默忽略（不崩、不建项）", () => {
    const state = applyBatch(initialEngineState, [toolResult("ghost", "x", true)]);
    assert.deepEqual(state.toolTimeline, {});
  });

  it("toolTimeline 每阶段滑动窗口上限 500（最早条目被丢弃）", () => {
    const calls: EngineEvent[] = [];
    for (let i = 0; i < 505; i++) calls.push(toolCall(`c${i}`, "t", "solving", i));
    const state = applyBatch(initialEngineState, calls);
    const items = state.toolTimeline["solving"] ?? [];
    assert.equal(items.length, 500);
    assert.equal(items[0].callId, "c5");
    assert.equal(items[499].callId, "c504");
  });

  it("model.delta 追加到 modelOutput（允许丢帧，仅展示）", () => {
    const state = applyBatch(initialEngineState, [delta("hello "), delta("world")]);
    assert.equal(state.modelOutput, "hello world");
  });

  it("modelOutput 超长截断保留末尾（8000 字符上限）", () => {
    const state = applyBatch(initialEngineState, [delta("x".repeat(9000))]);
    assert.equal(state.modelOutput.length, 8000);
  });

  it("混批（tool.call + stage.progress + model.delta）正确归约", () => {
    const state = applyBatch(initialEngineState, [
      toolCall("c1", "execute_code", "solving"),
      progress("solving", 0.5),
      delta("abc"),
    ]);
    assert.equal((state.toolTimeline["solving"] ?? []).length, 1);
    assert.equal(state.stageProgress["solving"], 0.5);
    assert.equal(state.modelOutput, "abc");
  });

  it("tool.call 不改变 artifacts/gates 引用（局部更新语义，避免无谓重渲染）", () => {
    const base = {
      ...initialEngineState,
      artifacts: [{ kind: "figure", sha256: "a".repeat(64), ts: "x" }],
      gates: [{ gate: "modeling", reason: "r", ts: "x" }],
    };
    const patch = applyBatch(base, [toolCall("c1", "t", "solving")]);
    assert.equal(patch.artifacts, base.artifacts);
    assert.equal(patch.gates, base.gates);
  });
});
