/**
 * SP3-4 纯逻辑测试（node:test）：自研 store、引擎事件批处理泵、
 * 事件归约（applyBatch）、Key 向导状态机。组件测试见 renderer/tests/*.test.tsx。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createStore, useStore } from "../renderer/src/storage/store.ts";
import { EngineEventPump } from "../renderer/src/engine/event-source.ts";
import { applyBatch, initialEngineState } from "../renderer/src/state/engine-slice.ts";
import { keyWizardReducer, initialWizard, reasonLabel } from "../renderer/src/state/key-wizard.ts";
import type { ErdosBridge } from "../renderer/src/bridges/bridge.ts";
import type { EngineEvent, StageProgressEvent } from "../shared/ipc.ts";

/** 测试桩桥：事件泵只依赖接口形状。 */
function pumpBridge(): ErdosBridge {
  return {
    invoke: async <T>(): Promise<T> => undefined as T,
    subscribe: () => () => {},
  };
}

function progress(
  taskId: string,
  stage: StageProgressEvent["stage"],
  value: number,
  step = 0,
): EngineEvent {
  return {
    trace_id: `t-${step}`,
    event: "stage.progress",
    task_id: taskId,
    stage,
    progress: value,
    timestamp: new Date(10_000 + step).toISOString(),
  };
}

describe("createStore 自研状态管理", () => {
  it("setState 产生新引用并通知订阅者；退订后不再通知", () => {
    const store = createStore<{ n: number }>({ n: 0 });
    const state0 = store.getState();
    const seen: number[] = [];
    const unsubscribe = store.subscribe(() => seen.push(store.getState().n));
    store.setState({ n: 1 });
    assert.notEqual(store.getState(), state0);
    store.setState({ n: 2 });
    unsubscribe();
    store.setState({ n: 3 });
    assert.deepEqual(seen, [1, 2]);
  });

  it("useStore 为 React 绑定类型（模块可加载即可）", () => {
    // 该用例保证 store.ts 与 react 类型解耦后仍可导入（Node 环境无 React 运行时钩子执行）
    assert.equal(typeof useStore, "function");
  });
});

describe("EngineEventPump 帧合并批处理", () => {
  it("同一窗口多条事件合并为一批广播", () => {
    const timers: Array<() => void> = [];
    const pump = new EngineEventPump(pumpBridge(), {
        batchMs: 16,
        setTimeoutFn: (fn) => {
          timers.push(fn);
          return timers.length - 1;
        },
        clearTimeoutFn: () => {},
      },
    );
    const batches: EngineEvent[][] = [];
    pump.onBatch((batch) => batches.push(batch));
    for (let i = 0; i < 5; i++) pump.push(progress("t1", "analysis", i / 5, i));
    assert.equal(batches.length, 0); // 未到帧边界
    const batch = pump.flush();
    assert.equal(batch.length, 5);
    assert.equal(batches.length, 1);
    assert.equal(pump.pendingCount(), 0);
  });

  it("100 条事件单帧合并为一次广播（流式压力：100 批/秒 不触发整页刷新）", () => {
    const pump = new EngineEventPump(pumpBridge(), {
        batchMs: 16,
        setTimeoutFn: () => 0,
        clearTimeoutFn: () => {},
      },
    );
    let broadcasts = 0;
    let total = 0;
    pump.onBatch((batch) => {
      broadcasts += 1;
      total += batch.length;
    });
    for (let i = 0; i < 100; i++) pump.push(progress("t1", "writing", i % 100 / 100, i));
    pump.flush();
    assert.equal(broadcasts, 1);
    assert.equal(total, 100);
  });

  it("空批不广播；stop 后不再调度", () => {
    const fired: Array<() => void> = [];
    const pump = new EngineEventPump(pumpBridge(), {
      batchMs: 16,
      setTimeoutFn: (fn) => {
        fired.push(fn);
        return fired.length - 1;
      },
      clearTimeoutFn: () => {},
    });
    let broadcasts = 0;
    pump.onBatch(() => (broadcasts += 1));
    pump.flush(); // 空队列
    assert.equal(broadcasts, 0);
    pump.push(progress("t1", "analysis", 0.5));
    assert.equal(fired.length, 1);
    pump.stop();
    pump.flush();
    assert.equal(broadcasts, 1); // stop 前已入队的事件仍在队列（不丢）
  });
});

describe("applyBatch 事件归约（局部更新语义）", () => {
  it("progress 只更新对应阶段进度与日志，产物与门禁不变", () => {
    const base = {
      ...initialEngineState,
      taskId: "t1",
      artifacts: [{ kind: "figure", sha256: "a".repeat(64), ts: "x" }],
      gates: [{ gate: "modeling", reason: "r", ts: "x" }],
    };
    const patch = applyBatch(base, [progress("t1", "analysis", 0.4)]);
    assert.equal(patch.stageProgress["analysis"], 0.4);
    assert.equal(patch.artifacts, base.artifacts); // 引用不变 → 无需重渲染
    assert.equal(patch.gates, base.gates);
    assert.equal(patch.log.length, 1);
  });

  it("artifact.ready 追加产物；gate.failed 追加评审意见；log 不断增长", () => {
    const events: EngineEvent[] = [
      {
        trace_id: "t2",
        event: "artifact.ready",
        task_id: "t1",
        artifact: "paper",
        sha256: "b".repeat(64),
        timestamp: new Date(10_001).toISOString(),
      },
      {
        trace_id: "t3",
        event: "gate.failed",
        task_id: "t1",
        gate: "solving",
        reason: "算法与模型不匹配",
        timestamp: new Date(10_002).toISOString(),
      },
    ];
    const patch = applyBatch(initialEngineState, events);
    assert.equal(patch.artifacts.length, 1);
    assert.equal(patch.artifacts[0].kind, "paper");
    assert.equal(patch.gates.length, 1);
    assert.ok(patch.gates[0].reason.includes("算法"));
    assert.equal(patch.log.length, 2);
  });
});

describe("Key 连通测试向导状态机", () => {
  it("input → save → saved → test-result 全流程与重置", () => {
    let state = keyWizardReducer(initialWizard, { type: "field", field: "alias", value: "DS" });
    state = keyWizardReducer(state, { type: "save" });
    assert.equal(state.stage, "saving");
    state = keyWizardReducer(state, { type: "saved" });
    assert.equal(state.stage, "testing");
    state = keyWizardReducer(state, {
      type: "test-result",
      result: { ok: false, reason: "unauthorized", detail: "401" },
    });
    assert.equal(state.stage, "done");
    assert.equal(state.result?.reason, "unauthorized");
    state = keyWizardReducer(state, { type: "reset" });
    assert.equal(state.stage, "input");
    assert.equal(state.alias, "DS"); // 重置保留输入
  });

  it("失败进入 error 态并保留可读原因", () => {
    const state = keyWizardReducer(initialWizard, { type: "fail", message: "网络不可达" });
    assert.equal(state.stage, "error");
    assert.equal(state.error, "网络不可达");
  });

  it("reasonLabel 覆盖四类失败分类与未检测态", () => {
    assert.equal(reasonLabel("unauthorized"), "鉴权失败（401）");
    assert.equal(reasonLabel("network"), "网络不可达");
    assert.equal(reasonLabel("balance"), "账户余额不足");
    assert.equal(reasonLabel("none"), "连通成功");
    // keysTest 真实化（W12）：无引擎环境不得显示「连通成功」
    assert.equal(reasonLabel("unverified"), "已保存（未检测连通）");
  });
});