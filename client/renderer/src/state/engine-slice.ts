/**
 * 引擎视图状态切片（SP3-4）：事件泵批次 → 纯函数归约 → store。
 * applyBatch 为纯函数（单测友好）；store 每帧最多 setState 一次（局部更新）。
 */

import type { EngineEvent, EngineEventName, StageName } from "../../../shared/ipc.ts";
import type { Store } from "../storage/store.ts";
import { createStore } from "../storage/store.ts";
import { EngineEventPump } from "../engine/event-source.ts";

export interface ArtifactView {
  kind: string;
  sha256: string;
  ts: string;
}

export interface GateFailureView {
  gate: string;
  reason: string;
  ts: string;
}

export interface EngineViewState {
  taskId: string | null;
  stage: StageName | null;
  running: boolean;
  /** 各阶段进度 0~1（stage.progress 局部更新）。 */
  stageProgress: Partial<Record<StageName, number>>;
  /** 产物列表（artifact.ready 追加）。 */
  artifacts: ArtifactView[];
  /** 门禁失败记录（gate.failed，US-004 展示评审意见）。 */
  gates: GateFailureView[];
  /** 事件日志（长日志；供虚拟列表渲染）。 */
  log: string[];
}

export const initialEngineState: EngineViewState = {
  taskId: null,
  stage: null,
  running: false,
  stageProgress: {},
  artifacts: [],
  gates: [],
  log: [],
};

function logLine(kind: EngineEventName, event: EngineEvent): string {
  return `[${event.timestamp}] ${kind} task=${event.task_id}`;
}

/** 纯函数：把一批引擎事件归约为全量状态（整对象克隆，引用不变字段保持原引用以便 React 跳过）。 */
export function applyBatch(state: EngineViewState, batch: EngineEvent[]): EngineViewState {
  const patch: EngineViewState = {
    taskId: state.taskId,
    stage: state.stage,
    running: state.running,
    stageProgress: { ...state.stageProgress },
    artifacts: state.artifacts,
    gates: state.gates,
    log: state.log,
  };
  for (const event of batch) {
    switch (event.event) {
      case "stage.progress": {
        patch.taskId = event.task_id;
        patch.stage = event.stage;
        patch.running = true;
        patch.stageProgress = { ...patch.stageProgress, [event.stage]: event.progress };
        patch.log = [...patch.log, `${logLine(event.event, event)} ${event.stage} ${Math.round(event.progress * 100)}%`];
        break;
      }
      case "artifact.ready": {
        patch.artifacts = [
          ...patch.artifacts,
          { kind: event.artifact, sha256: event.sha256, ts: event.timestamp },
        ];
        patch.log = [...patch.log, `${logLine(event.event, event)} ${event.artifact} ${event.sha256.slice(0, 8)}`];
        break;
      }
      case "gate.failed": {
        patch.gates = [...patch.gates, { gate: event.gate, reason: event.reason, ts: event.timestamp }];
        patch.log = [...patch.log, `${logLine(event.event, event)} ${event.gate} ${event.reason}`];
        break;
      }
    }
  }
  return patch;
}

export interface EngineStoreHandle {
  store: Store<EngineViewState>;
  pump: EngineEventPump;
}

/** 装配：事件泵 → applyBatch → store。返回句柄（含 pump 供演示注入/停止）。 */
export function createEngineStore(pump: EngineEventPump): EngineStoreHandle {
  const store = createStore<EngineViewState>(initialEngineState);
  pump.onBatch((batch) => store.setState(applyBatch(store.getState(), batch)));
  return { store, pump };
}