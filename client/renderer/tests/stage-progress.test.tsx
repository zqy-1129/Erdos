/**
 * 四阶段进度组件测试（SP3-4）：
 * - stage.progress 局部更新（进度文本变化、产物列表不受影响）；
 * - 100 事件/秒合并批量 + 渲染预算（流式无卡顿）。
 */

import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { StageProgress } from "../src/components/stage-progress.tsx";
import { applyBatch, initialEngineState, type EngineViewState } from "../src/state/engine-slice.ts";
import { createStore } from "../src/storage/store.ts";
import type { EngineEvent } from "../../shared/ipc.ts";

afterEach(cleanup);

function makeEvents(): EngineEvent[] {
  const events: EngineEvent[] = [];
  const stages: Array<"analysis" | "modeling" | "solving" | "writing"> = ["analysis", "modeling", "solving", "writing"];
  for (let i = 0; i < 100; i++) {
    const timestamp = new Date(10_000 + i).toISOString();
    if (i % 10 === 0) {
      events.push({
        trace_id: `t-${i}`,
        event: "artifact.ready",
        task_id: "t1",
        artifact: "figure",
        sha256: "c".repeat(64),
        timestamp,
      });
    } else {
      events.push({
        trace_id: `t-${i}`,
        event: "stage.progress",
        task_id: "t1",
        stage: stages[i % 4],
        progress: i / 100,
        timestamp,
      });
    }
  }
  return events;
}

describe("StageProgress 局部更新与流式渲染", () => {
  it("progress 更新只改进度文本；产物/门禁/日志不受影响", () => {
    const store = createStore<EngineViewState>(initialEngineState);
    render(<StageProgress engine={store} />);
    expect(screen.getByText("分析")).toBeTruthy();

    act(() => {
      store.setState({ stageProgress: { analysis: 0.5 }, stage: "analysis", running: true, taskId: "t1" });
    });
    expect(screen.getByText("50%")).toBeTruthy();

    // 产物追加不影响已有进度（applyBatch 引用不变语义）
    act(() => {
      store.setState(
        applyBatch(store.getState(), [
          {
            trace_id: "t-x",
            event: "artifact.ready",
            task_id: "t1",
            artifact: "paper",
            sha256: "d".repeat(64),
            timestamp: new Date(20_000).toISOString(),
          },
        ]),
      );
    });
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText(/sha256:/)).toBeTruthy();
  });

  it("100 条事件单帧渲染预算低于 500ms（流式无卡顿验收）", () => {
    const store = createStore<EngineViewState>(initialEngineState);
    render(<StageProgress engine={store} />);
    const started = performance.now();
    act(() => {
      store.setState(applyBatch(store.getState(), makeEvents())); // 一帧一批
    });
    const elapsed = performance.now() - started;
    expect(elapsed).toBeLessThan(500);
    // 日志总数 100（虚拟列表仅渲染窗口内行，页面仍可交互）
    expect(screen.getByText(/事件日志（100）/)).toBeTruthy();
    // 100 条事件中有 10 条 artifact.ready（i%10===0）；产物面板只保留最近 20 条（全部展示）
    const artifacts = screen.getAllByText(/sha256:/);
    expect(artifacts.length).toBe(10);
  });

  it("门禁失败展示评审意见 + rubric 可查", () => {
    const store = createStore<EngineViewState>(initialEngineState);
    render(<StageProgress engine={store} />);
    act(() => {
      store.setState(
        applyBatch(store.getState(), [
          {
            trace_id: "t-g",
            event: "gate.failed",
            task_id: "t1",
            gate: "modeling",
            reason: "目标函数未包含变量约束",
            timestamp: new Date(30_000).toISOString(),
          },
        ]),
      );
    });
    expect(screen.getAllByText(/目标函数未包含变量约束/).length).toBeGreaterThan(0); // 门禁卡片 + 日志各一处
    act(() => {
      screen.getByRole("button", { name: /门禁评分规则/ }).click();
    });
    expect(screen.getByText(/目标函数含变量约束/)).toBeTruthy(); // rubric 内容
  });
});