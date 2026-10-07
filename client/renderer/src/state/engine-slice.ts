/**
 * 引擎视图状态切片（SP3-4）：事件泵批次 → 纯函数归约 → store。
 * applyBatch 为纯函数（单测友好）；store 每帧最多 setState 一次（局部更新）。
 * C1 扩展（FE-TOOLUI/FE-APPROVE）：toolTimeline / modelOutput / gateRetries / 状态快照同步。
 */

import type { EngineEvent, EngineEventName, GetStatusResult, StageName } from "../../../shared/ipc.ts";
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

/** 工具调用时间线条目（tool.call 建项，tool.result 按 callId 回填结果）。 */
export interface ToolTimelineItem {
  callId: string;
  tool: string;
  argsSummary: string;
  startedAt: string;
  /** tool.result 到达前为 null（进行中）。 */
  ok: boolean | null;
  durationMs: number | null;
  resultRef: string | null;
  error: string | null;
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
  /** 各门禁已失败次数（DEC-008：重试 ≤3 转人工，UI 展示 x/3）。 */
  gateRetries: Record<string, number>;
  /** 工具调用时间线（每阶段滑动窗口 500 条，防长任务内存膨胀）。 */
  toolTimeline: Partial<Record<StageName, ToolTimelineItem[]>>;
  /** model.delta 拼接的当前阶段输出（允许丢帧，仅展示；截断保留末尾）。 */
  modelOutput: string;
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
  gateRetries: {},
  toolTimeline: {},
  modelOutput: "",
  log: [],
};

function logLine(kind: EngineEventName, event: EngineEvent): string {
  return `[${event.timestamp}] ${kind} task=${event.task_id}`;
}

/** 工具时间线每阶段滑动窗口上限（防长任务内存膨胀，客户端方案 §6.2）。 */
const TOOL_WINDOW = 500;
/** model.delta 拼接输出截断上限（仅展示，保留末尾）。 */
const MODEL_OUTPUT_MAX = 8000;

/** 纯函数：把一批引擎事件归约为全量状态（整对象克隆，引用不变字段保持原引用以便 React 跳过）。 */
export function applyBatch(state: EngineViewState, batch: EngineEvent[]): EngineViewState {
  const patch: EngineViewState = {
    taskId: state.taskId,
    stage: state.stage,
    running: state.running,
    stageProgress: { ...state.stageProgress },
    artifacts: state.artifacts,
    gates: state.gates,
    gateRetries: state.gateRetries,
    toolTimeline: state.toolTimeline,
    modelOutput: state.modelOutput,
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
        patch.gateRetries = {
          ...patch.gateRetries,
          [event.gate]: (patch.gateRetries[event.gate] ?? 0) + 1,
        };
        patch.log = [...patch.log, `${logLine(event.event, event)} ${event.gate} ${event.reason}`];
        break;
      }
      case "tool.call": {
        // 建项（ok=null 进行中）；args_summary 已由引擎脱敏（禁止题面正文）
        patch.stage = event.stage;
        const list = patch.toolTimeline[event.stage] ?? [];
        const item: ToolTimelineItem = {
          callId: event.call_id,
          tool: event.tool,
          argsSummary: event.args_summary,
          startedAt: event.timestamp,
          ok: null,
          durationMs: null,
          resultRef: null,
          error: null,
        };
        patch.toolTimeline = {
          ...patch.toolTimeline,
          [event.stage]: [...list, item].slice(-TOOL_WINDOW),
        };
        patch.log = [...patch.log, `${logLine(event.event, event)} ${event.tool}`];
        break;
      }
      case "tool.result": {
        // 按 callId 回填结果（tool.result 无 stage 字段，跨阶段查找）
        const callId = event.call_id;
        const next = { ...patch.toolTimeline };
        for (const [stage, list] of Object.entries(next) as Array<[StageName, ToolTimelineItem[]]>) {
          const idx = list.findIndex((t) => t.callId === callId);
          if (idx >= 0) {
            next[stage] = list.map((t, i) =>
              i === idx
                ? {
                    ...t,
                    ok: event.ok,
                    durationMs: event.duration_ms,
                    resultRef: event.result_ref ?? null,
                    error: event.error ?? null,
                  }
                : t,
            );
            break;
          }
        }
        patch.toolTimeline = next;
        patch.log = [...patch.log, `${logLine(event.event, event)} ${event.tool} ${event.ok ? "ok" : "fail"}`];
        break;
      }
      case "model.delta": {
        // token 流：追加到输出区，允许丢帧；截断保留末尾（防长任务内存膨胀）
        patch.modelOutput = (patch.modelOutput + event.delta).slice(-MODEL_OUTPUT_MAX);
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

/** 纯函数：把 get_status 快照同步到视图状态（冲突/轮询后收敛 running/stage/taskId）。 */
export function applyStatusSnapshot(state: EngineViewState, status: GetStatusResult): EngineViewState {
  const task = status.task;
  const orch = status.orchestrator;
  return {
    ...state,
    taskId: task?.task_id ?? orch?.task_id ?? state.taskId,
    stage: task?.stage ?? orch?.current_stage ?? state.stage,
    running: status.engine === "running",
  };
}

/** 判断 answer_gate 冲突错误（引擎"门禁冲突"经 RPC 收敛为 code -32602，客户端识别 message 含"冲突"）。 */
export function isGateConflict(error: unknown): boolean {
  return error instanceof Error && /冲突/.test(error.message);
}

/** 装配：事件泵 → applyBatch → store。返回句柄（含 pump 供演示注入/停止）。 */
export function createEngineStore(pump: EngineEventPump): EngineStoreHandle {
  const store = createStore<EngineViewState>(initialEngineState);
  pump.onBatch((batch) => store.setState(applyBatch(store.getState(), batch)));
  return { store, pump };
}
