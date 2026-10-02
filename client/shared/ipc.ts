/**
 * 客户端 IPC 通道类型定义（SP3-1）
 *
 * 对齐 contracts/engine-rpc.schema.json：
 * - 6 个 JSON-RPC 2.0 方法（start_stage/pause/resume/cancel/get_status/answer_gate）
 * - 3 个 NDJSON 事件（stage.progress/artifact.ready/gate.failed）
 *
 * 通信走 stdio 管道，主进程经 stdin 发请求、读 stdout 响应与事件（按字段分流）。
 */

// ---------------------------------------------------------------------------
// 枚举与基础类型
// ---------------------------------------------------------------------------
export type StageName = "analysis" | "modeling" | "solving" | "writing";
export type EngineStatus = "idle" | "running" | "paused" | "stopped";
export type StageStatus =
  | "pending"
  | "running"
  | "paused"
  | "done"
  | "failed"
  | "cancelled";

export type RpcId = string | number | null;

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 信封
// ---------------------------------------------------------------------------
export interface RpcRequest {
  jsonrpc: "2.0";
  id: RpcId;
  method: RpcMethod;
  params: Record<string, unknown>;
}

export interface RpcResponse {
  jsonrpc: "2.0";
  id: RpcId;
  result?: unknown;
  error?: { code: number; message: string };
}

// ---------------------------------------------------------------------------
// 6 个 RPC 方法
// ---------------------------------------------------------------------------
export type RpcMethod =
  | "start_stage"
  | "pause"
  | "resume"
  | "cancel"
  | "get_status"
  | "answer_gate";

export interface StartStageParams {
  task_id: string;
  stage: StageName;
}
export interface StartStageResult {
  task_id: string;
  stage: StageName;
  status: StageStatus;
}

export interface TaskActionParams {
  task_id: string;
}
export interface TaskActionResult {
  task_id: string;
  status: EngineStatus;
}

export interface AnswerGateParams {
  task_id: string;
  gate: string;
  decision: "pass" | "reject";
  feedback?: string;
}
export interface AnswerGateResult {
  task_id: string;
  gate: string;
  decision: "pass" | "reject";
  action?: "next_stage" | "retry_stage" | "complete";
}

export interface TaskState {
  task_id: string;
  stage: StageName;
  status: StageStatus;
}

export interface GetStatusResult {
  engine: EngineStatus;
  task: TaskState | null;
  orchestrator?: {
    task_id: string;
    current_stage: StageName;
    stage_index: number;
    stages: Record<string, { status: string; step: number }>;
    gate_decision: string | null;
  };
}

// ---------------------------------------------------------------------------
// 3 个 NDJSON 事件
// ---------------------------------------------------------------------------
export type EngineEventName = "stage.progress" | "artifact.ready" | "gate.failed";

export interface StageProgressEvent {
  trace_id: string;
  event: "stage.progress";
  task_id: string;
  stage: StageName;
  progress: number; // 0~1
  timestamp: string;
}

export interface ArtifactReadyEvent {
  trace_id: string;
  event: "artifact.ready";
  task_id: string;
  artifact: "figure" | "table" | "data" | "code" | "paper";
  sha256: string; // 64 字符 hex
  timestamp: string;
}

export interface GateFailedEvent {
  trace_id: string;
  event: "gate.failed";
  task_id: string;
  gate: string;
  reason: string;
  timestamp: string;
}

export type EngineEvent = StageProgressEvent | ArtifactReadyEvent | GateFailedEvent;

// ---------------------------------------------------------------------------
// 主进程 ↔ 引擎 通道白名单（preload 桥只暴露注册通道）
// ---------------------------------------------------------------------------
export const ENGINE_IPC_CHANNELS = [
  "engine:start_stage",
  "engine:pause",
  "engine:resume",
  "engine:cancel",
  "engine:get_status",
  "engine:answer_gate",
  "engine:event", // 引擎 → 渲染层事件转发
] as const;

export type EngineIpcChannel = (typeof ENGINE_IPC_CHANNELS)[number];
