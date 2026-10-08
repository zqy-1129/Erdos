/**
 * 引擎事件泵（SP3-4）：订阅 bridge 的 engine:event → 帧合并批处理 → 监听器广播。
 *
 * 流式无卡顿（验收：100 事件/s 时 UI 可交互）：
 * 高频 NDJSON 事件不逐条触发 React 渲染，而是按帧（默认 16ms）合并为一批，
 * 每帧至多一次 setState（局部更新，不整页刷新）；定时器可注入（测试用假时钟）。
 */

import type { ErdosBridge } from "../bridges/bridge.ts";
import type { EngineEvent } from "../../../shared/ipc.ts";

export interface EventPumpOptions {
  /** 批合并窗口（ms）。 */
  batchMs?: number;
  setTimeoutFn?: (fn: () => void, ms: number) => unknown;
  clearTimeoutFn?: (handle: unknown) => void;
}

export type EngineEventBatch = EngineEvent[];

const DEFAULT_BATCH_MS = 16;

export class EngineEventPump {
  private readonly bridge: ErdosBridge;
  private queue: EngineEvent[] = [];
  private timer: unknown = null;
  private started = false;
  private unsubscribeBridge: (() => void) | null = null;
  private readonly listeners = new Set<(batch: EngineEventBatch) => void>();
  private readonly batchMs: number;
  private readonly setTimeoutFn: (fn: () => void, ms: number) => unknown;
  private readonly clearTimeoutFn: (handle: unknown) => void;

  constructor(bridge: ErdosBridge, options: EventPumpOptions = {}) {
    this.bridge = bridge;
    this.batchMs = options.batchMs ?? DEFAULT_BATCH_MS;
    this.setTimeoutFn = options.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimeoutFn = options.clearTimeoutFn ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  /** 开始订阅桥事件流；返回停止函数（幂等）。 */
  start(): () => void {
    if (this.started) return () => this.stop();
    this.started = true;
    this.unsubscribeBridge = this.bridge.subscribe("engine:event", (payload) => {
      this.push(payload as EngineEvent);
    });
    return () => this.stop();
  }

  stop(): void {
    this.started = false;
    if (this.unsubscribeBridge) {
      this.unsubscribeBridge();
      this.unsubscribeBridge = null;
    }
    if (this.timer !== null) {
      this.clearTimeoutFn(this.timer);
      this.timer = null;
    }
  }

  /** 入队一条事件（bridge 订阅或演示/测试直接注入）。 */
  push(event: EngineEvent): void {
    this.queue.push(event);
    this.schedule();
  }

  /** 帧边界：把当前队列作为一批广播给所有监听器。 */
  flush(): EngineEventBatch {
    if (this.timer !== null) {
      this.clearTimeoutFn(this.timer);
      this.timer = null;
    }
    const batch = this.queue;
    this.queue = [];
    if (batch.length === 0) return batch;
    for (const listener of [...this.listeners]) listener(batch);
    return batch;
  }

  /** 订阅批处理结果；返回退订函数。 */
  onBatch(listener: (batch: EngineEventBatch) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  pendingCount(): number {
    return this.queue.length;
  }

  private schedule(): void {
    if (this.timer !== null) return;
    this.timer = this.setTimeoutFn(() => {
      this.timer = null;
      this.flush();
    }, this.batchMs);
  }
}