/**
 * 遥测 SDK（SP3-5）：采集 → 校验 → 隐私过滤 → 本地 outbox → 批量上报 → 断网重试。
 *
 * 硬约束（PRD）：
 * - 本地缓冲 + 批量上报 + 断网重试 —— 比赛现场断网时数据不丢失；
 * - 隐私红线：props 禁止题面/API Key/文件路径等字段，采集时即剥离（永不落盘/上报）。
 * 契约对齐：contracts/telemetry.schema.json + 服务端 /v1/telemetry/events（批 ≤1000）。
 */

import { CloudHttpClient, type TokenProvider } from "../cloud/http.ts";

/** 事件名白名单（contracts/telemetry.schema.json events）。 */
export const VALID_EVENT_NAMES = new Set([
  "page_view",
  "feature_click",
  "stage_start",
  "stage_success",
  "stage_fail",
  "gate_retry",
  "order_created",
  "pay_success",
  "app_error",
]);

/** 违禁字段（隐私红线）：采集时剥离，永不落盘/上报。 */
export const FORBIDDEN_PROPS = new Set([
  "prompt_zh",
  "prompt_en",
  "paper_content",
  "api_key",
  "file_path",
  "model_output",
  "trail_detail",
]);

/** 服务端单批容量上限（/v1/telemetry/events max 1000）。 */
export const MAX_BATCH_SIZE = 1000;

/** distinct_id 长度上限（contracts definitions.distinctId）。 */
const DISTINCT_ID_MAX = 128;

/** 已过滤、待上报的事件。 */
export interface QueuedEvent {
  event_name: string;
  distinct_id: string;
  props: Record<string, unknown>;
  app_version: string;
  os: string;
  channel: string;
}

/** 采集草稿（props 可含违禁字段，SDK 负责剥离）。 */
export interface TelemetryEventDraft {
  event_name: string;
  distinct_id: string;
  props?: Record<string, unknown>;
  app_version?: string;
  os?: string;
  channel?: string;
}

/** Outbox 行（对齐 telemetry_outbox 表结构，v2 含 app_version/os/channel 列）。 */
export interface OutboxRow {
  id: number;
  event_name: string;
  distinct_id: string;
  /** 过滤后 props 的 JSON 文本。 */
  props: string;
  app_version: string;
  os: string;
  channel: string;
  created_at: string;
}

/** Outbox 存储（内存实现为参考；SP3-4 接 migrations v1/v2 telemetry_outbox SQLite 实现）。 */
export interface OutboxStore {
  add(rows: Omit<OutboxRow, "id">[]): void;
  list(limit: number): OutboxRow[];
  remove(ids: number[]): void;
  size(): number;
}

/** 内存 outbox（进程生命周期内缓冲；SP3-4 换 SQLite 实现落盘）。 */
export class InMemoryOutboxStore implements OutboxStore {
  private nextId = 1;
  private rows: OutboxRow[] = [];

  add(rows: Omit<OutboxRow, "id">[]): void {
    for (const row of rows) {
      this.rows.push({ ...row, id: this.nextId++ });
    }
  }
  list(limit: number): OutboxRow[] {
    return this.rows.slice(0, limit);
  }
  remove(ids: number[]): void {
    const keep = new Set(ids);
    this.rows = this.rows.filter((row) => !keep.has(row.id));
  }
  size(): number {
    return this.rows.length;
  }
}

/** 上报结果（对齐服务端 IngestView）。 */
export interface UploadResult {
  accepted: number;
  rejected: number;
  reasons: string[];
}

/** 上报通道（Cloud 实现对接 /v1/telemetry/events）。 */
export interface TelemetryUploader {
  upload(events: QueuedEvent[]): Promise<UploadResult>;
}

/** 云端上报器：POST /v1/telemetry/events（批 ≤1000，信封解包）。 */
export class CloudTelemetryUploader implements TelemetryUploader {
  private readonly http: CloudHttpClient;

  constructor(http: CloudHttpClient) {
    this.http = http;
  }

  async upload(events: QueuedEvent[]): Promise<UploadResult> {
    const value = await this.http.post<Record<string, unknown>>("/v1/telemetry/events", {
      events,
    });
    return {
      accepted: Number(value["accepted"] ?? 0),
      rejected: Number(value["rejected"] ?? 0),
      reasons: Array.isArray(value["reasons"]) ? (value["reasons"] as string[]) : [],
    };
  }
}

export interface TelemetryOptions {
  uploader: TelemetryUploader;
  store?: OutboxStore;
  /** 单批容量（≤1000，默认 200）。 */
  batchSize?: number;
  /** 本地队列达到该值时自动 flush（默认 batchSize）。 */
  autoFlushAt?: number;
  /** flush 单批重试次数（默认 3）。 */
  maxAttempts?: number;
  /** 退避基数 ms（默认 500，指数退避）。 */
  backoffMs?: number;
  /** 等待注入（测试用，默认 setTimeout）。 */
  sleep?: (ms: number) => Promise<void>;
  /** 自动 flush 失败回调（后台触发，不阻塞采集）。 */
  onAutoFlushError?: (error: unknown) => void;
}

/** props 隐私过滤：剥离违禁字段（其余字段原样保留）。 */
export function sanitizeProps(props: Record<string, unknown>): Record<string, unknown> {
  const cleaned: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(props)) {
    if (!FORBIDDEN_PROPS.has(key)) cleaned[key] = value;
  }
  return cleaned;
}

function toQueued(row: OutboxRow): QueuedEvent {
  let props: Record<string, unknown>;
  try {
    props = JSON.parse(row.props) as Record<string, unknown>;
  } catch {
    props = {}; // 损坏行仅丢失 props，事件本身仍上报
  }
  return {
    event_name: row.event_name,
    distinct_id: row.distinct_id,
    props,
    app_version: row.app_version,
    os: row.os,
    channel: row.channel,
  };
}

const DEFAULT_BATCH_SIZE = 200;
const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_BACKOFF_MS = 500;

/** 遥测 SDK：采集即落 outbox（本地不丢），flush 批量上报（失败保留重试）。 */
export class TelemetrySdk {
  private readonly uploader: TelemetryUploader;
  private readonly store: OutboxStore;
  private readonly batchSize: number;
  private readonly autoFlushAt: number;
  private readonly maxAttempts: number;
  private readonly backoffMs: number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly onAutoFlushError?: (error: unknown) => void;

  constructor(options: TelemetryOptions) {
    this.uploader = options.uploader;
    this.store = options.store ?? new InMemoryOutboxStore();
    this.batchSize = Math.min(Math.max(1, options.batchSize ?? DEFAULT_BATCH_SIZE), MAX_BATCH_SIZE);
    this.autoFlushAt = options.autoFlushAt ?? this.batchSize;
    this.maxAttempts = Math.max(1, options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS);
    this.backoffMs = options.backoffMs ?? DEFAULT_BACKOFF_MS;
    this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.onAutoFlushError = options.onAutoFlushError;
  }

  /**
   * 采集事件：白名单校验 + 隐私过滤后写入本地 outbox。
   * 非法事件（事件名不在白名单/ident 为空）本地丢弃返回 null，永不上报。
   * 达到 autoFlushAt 自动触发后台 flush。
   */
  capture(draft: TelemetryEventDraft): QueuedEvent | null {
    if (!VALID_EVENT_NAMES.has(draft.event_name)) return null;
    const distinctId = draft.distinct_id.trim().slice(0, DISTINCT_ID_MAX);
    if (!distinctId) return null;
    const event: QueuedEvent = {
      event_name: draft.event_name,
      distinct_id: distinctId,
      props: sanitizeProps(draft.props ?? {}),
      app_version: draft.app_version ?? "",
      os: draft.os ?? "",
      channel: draft.channel ?? "stable",
    };
    this.store.add([
      {
        event_name: event.event_name,
        distinct_id: event.distinct_id,
        props: JSON.stringify(event.props),
        app_version: event.app_version,
        os: event.os,
        channel: event.channel,
        created_at: new Date().toISOString(),
      },
    ]);
    if (this.store.size() >= this.autoFlushAt) {
      void this.flush().catch((error: unknown) => this.onAutoFlushError?.(error));
    }
    return event;
  }

  /** 本地待上报事件数。 */
  queued(): number {
    return this.store.size();
  }

  /**
   * 批量上报本地缓冲：逐批（≤batchSize）上传，服务端受理后移除本批；
   * 单批网络失败按退避重试 maxAttempts 次，仍失败则保留 outbox 并抛出（断网不丢数据）。
   */
  async flush(): Promise<UploadResult> {
    const totals: UploadResult = { accepted: 0, rejected: 0, reasons: [] };
    let rows = this.store.list(this.batchSize);
    while (rows.length > 0) {
      const result = await this.uploadWithRetry(rows.map(toQueued));
      this.store.remove(rows.map((row) => row.id)); // 服务端已受理（含拒绝），本地移除
      totals.accepted += result.accepted;
      totals.rejected += result.rejected;
      totals.reasons.push(...result.reasons);
      rows = this.store.list(this.batchSize);
    }
    return totals;
  }

  private async uploadWithRetry(events: QueuedEvent[]): Promise<UploadResult> {
    let lastError: unknown = null;
    for (let attempt = 1; attempt <= this.maxAttempts; attempt++) {
      if (attempt > 1) {
        await this.sleep(this.backoffMs * 2 ** (attempt - 2));
      }
      try {
        return await this.uploader.upload(events);
      } catch (error) {
        lastError = error;
      }
    }
    throw new Error(
      `遥测上报失败（重试 ${this.maxAttempts} 次）：${lastError instanceof Error ? lastError.message : String(lastError)}`,
    );
  }
}

export interface CloudTelemetryOptions {
  baseUrl: string;
  /** 可选登录态（事件端点公开，登录用户附带 Bearer）。 */
  getToken?: TokenProvider;
  store?: OutboxStore;
  batchSize?: number;
  autoFlushAt?: number;
  maxAttempts?: number;
  backoffMs?: number;
  timeoutMs?: number;
  maxRetries?: number;
  onAutoFlushError?: (error: unknown) => void;
}

/** 装配云端遥测：CloudHttpClient（重试/退避）→ /v1/telemetry/events 上报器。 */
export function createCloudTelemetry(options: CloudTelemetryOptions): TelemetrySdk {
  const http = new CloudHttpClient({
    baseUrl: options.baseUrl,
    getToken: options.getToken,
    timeoutMs: options.timeoutMs,
    maxRetries: options.maxRetries,
  });
  return new TelemetrySdk({
    uploader: new CloudTelemetryUploader(http),
    store: options.store,
    batchSize: options.batchSize,
    autoFlushAt: options.autoFlushAt,
    maxAttempts: options.maxAttempts,
    backoffMs: options.backoffMs,
    onAutoFlushError: options.onAutoFlushError,
  });
}