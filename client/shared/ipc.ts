/**
 * 客户端 IPC 通道类型定义（SP3-1）
 *
 * 对齐 contracts/engine-rpc.schema.json：
 * - 10 个 JSON-RPC 2.0 方法（v1：start_stage/pause/resume/cancel/get_status/answer_gate；
 *   CT-V2 增量：initialize/provider_test/events_replay/task_create）
 * - 6 个 NDJSON 事件（v1：stage.progress/artifact.ready/gate.failed；CT-V2 增量：tool.call/tool.result/model.delta）
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
/**
 * RPC 方法名运行时常量（与 contracts/engine-rpc.schema.json methods 对齐，
 * 由 tests/contract-ipc.test.ts 守护零漂移；类型从常量派生保证单一来源）。
 */
export const RPC_METHODS = [
  "start_stage",
  "pause",
  "resume",
  "cancel",
  "get_status",
  "answer_gate",
  "initialize",
  "provider_test",
  "events_replay",
  "task_create",
] as const;

export type RpcMethod = (typeof RPC_METHODS)[number];

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
// 5 个 NDJSON 事件（v1 三类 + CT-V2 增量 tool.call/tool.result）
// ---------------------------------------------------------------------------
/** 引擎事件名运行时常量（契约单一来源，CI 守护零漂移）。 */
export const ENGINE_EVENT_NAMES = [
  "stage.progress",
  "artifact.ready",
  "gate.failed",
  "tool.call",
  "tool.result",
  "model.delta",
] as const;

export type EngineEventName = (typeof ENGINE_EVENT_NAMES)[number];

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

export interface ToolCallEvent {
  trace_id: string;
  event: "tool.call";
  task_id: string;
  stage: StageName;
  call_id: string;
  tool: string;
  /** 脱敏后参数摘要（≤512 字符，禁止题面正文/Key） */
  args_summary: string;
  timestamp: string;
}

export interface ToolResultEvent {
  trace_id: string;
  event: "tool.result";
  task_id: string;
  call_id: string;
  tool: string;
  ok: boolean;
  duration_ms: number;
  /** 产物引用（sha256 或 artifact 路径引用） */
  result_ref?: string;
  /** 模型可读结构化错误（ok=false 时） */
  error?: string;
  timestamp: string;
}

export interface ModelDeltaEvent {
  trace_id: string;
  event: "model.delta";
  task_id: string;
  /** 节流合并后的 token 增量（≤512 字符；允许丢帧，不进 replay） */
  delta: string;
  timestamp: string;
}

export type EngineEvent =
  | StageProgressEvent
  | ArtifactReadyEvent
  | GateFailedEvent
  | ToolCallEvent
  | ToolResultEvent
  | ModelDeltaEvent;

// ---------------------------------------------------------------------------
// CT-V2 增量方法类型（W14：握手 / 探测 / 事件补发）
// ---------------------------------------------------------------------------
export interface InitializeParams {
  client_protocol_version?: number;
}
export interface InitializeResult {
  protocol_version: number;
  engine_version: string;
  compatible: boolean;
  capabilities?: {
    tool_mode: "tool_loop" | "stage_level";
    isolation_mode: "docker" | "subprocess" | "unknown";
  };
}

export interface ProviderTestParams {
  base_url: string;
  model: string;
  provider?: string;
}
export interface ProviderTestResult {
  ok: boolean;
  models_endpoint: boolean;
  tool_mode: "tool_loop" | "stage_level";
  probe_source?: string;
}

export interface EventsReplayParams {
  after_seq: number;
  limit?: number;
  task_id?: string;
}
export interface EventsReplayResult {
  events: EngineEvent[];
  last_seq: number;
}

// EN-PAPER：任务题面登记（start_stage 前必须调用；题面仅引擎内存持有）
export interface TaskCreateParams {
  task_id: string;
  title: string;
  problem_text: string;
  notes?: string;
}
export interface TaskCreateResult {
  task_id: string;
  status: "created";
}

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
  "engine:initialize",
  "engine:provider_test",
  "engine:events_replay",
  "engine:task_create",
  "engine:event", // 引擎 → 渲染层事件转发
] as const;

export type EngineIpcChannel = (typeof ENGINE_IPC_CHANNELS)[number];
