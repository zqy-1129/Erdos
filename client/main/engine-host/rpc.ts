/**
 * 引擎 JSON-RPC client（SP3-1 / FE-HOST W3）：stdin 发请求、stdout 分流、stderr 脱敏落盘。
 *
 * 协议源：main/engine-protocol.md + contracts/engine-rpc.schema.json + shared/ipc.ts。
 * - 请求 id 采用 `ui-<单调序列>`（engine-protocol §6 风格）；
 * - stdout 逐行 JSON：含 `error`/`result` → 匹配 pending map；含 `event` 字段 → 白名单校验 →
 *   onEvent 转发（EC-N4：未知事件拒绝并计数）；
 * - stderr 不承载协议，先过 secret-masker 再交 onStderr 落盘（滚动上限由宿主控制）；
 * - 单行 1MiB 上限（EC-S6：超出即断开并记协议错误）。
 */
import { createInterface } from "node:readline";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { maskText } from "../secret-masker.ts";
import {
  ENGINE_EVENT_NAMES,
  type EngineEvent,
  type RpcMethod,
  type RpcRequest,
} from "../../shared/ipc.ts";

/** 单行 stdout 上限（EC-S6）。 */
const MAX_LINE_BYTES = 1024 * 1024; // 1MiB
/** 控制类方法默认超时（start_stage 只等受理确认，同样适用；引擎同步返回 accepted）。 */
const DEFAULT_TIMEOUT_MS = 5000;

interface PendingEntry {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export interface EngineRpcCallbacks {
  /** 白名单校验通过后的引擎事件（转发渲染层）。 */
  onEvent: (event: EngineEvent) => void;
  /** 已脱敏的引擎 stderr 单行（宿主落盘）。 */
  onStderr: (line: string) => void;
  /** 协议错误（坏行/超限/未知事件），宿主计数或断链。 */
  onProtocolError: (err: Error) => void;
}

export class EngineRpcClient {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly callbacks: EngineRpcCallbacks;
  private readonly pending = new Map<number, PendingEntry>();
  private seq = 0;
  private closed = false;
  private unknownEventCount = 0;

  constructor(child: ChildProcessWithoutNullStreams, callbacks: EngineRpcCallbacks) {
    this.child = child;
    this.callbacks = callbacks;
    this.attachStdout();
    this.attachStderr();
  }

  /** 发送 JSON-RPC 请求并等待响应（按 id 匹配）。 */
  invoke<T = unknown>(method: RpcMethod, params: Record<string, unknown>, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
    if (this.closed) {
      return Promise.reject(new Error("ENGINE_RESTART：引擎已关闭，请重试或恢复任务"));
    }
    const id = this.seq++;
    const request: RpcRequest = { jsonrpc: "2.0", id, method, params };
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`引擎请求超时（${method}，${timeoutMs}ms）`));
      }, timeoutMs);
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
      try {
        this.child.stdin.write(`${JSON.stringify(request)}\n`);
      } catch (err) {
        this.pending.delete(id);
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  /** 关闭 client：清空所有 pending（以 ENGINE_RESTART 失败），停读流。 */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(new Error("ENGINE_RESTART：引擎进程退出，in-flight 请求失败"));
    }
    this.pending.clear();
  }

  /** 未知事件计数（EC-N4：告警口径）。 */
  get unknownEventTotal(): number {
    return this.unknownEventCount;
  }

  private attachStdout(): void {
    const lines = createInterface({ input: this.child.stdout });
    lines.on("line", (raw) => {
      if (this.closed) return;
      if (Buffer.byteLength(raw, "utf8") > MAX_LINE_BYTES) {
        this.callbacks.onProtocolError(new Error("引擎 stdout 单行超过 1MiB，按协议错误处理"));
        return;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        this.callbacks.onProtocolError(new Error(`引擎 stdout 坏行（非 JSON）：${raw.slice(0, 80)}`));
        return;
      }
      this.dispatch(parsed as Record<string, unknown>);
    });
    lines.on("error", (err) => {
      if (!this.closed) this.callbacks.onProtocolError(err);
    });
  }

  private dispatch(line: Record<string, unknown>): void {
    // 事件行：含 event 字段
    if (typeof line.event === "string") {
      const name = line.event;
      if (!(ENGINE_EVENT_NAMES as readonly string[]).includes(name)) {
        this.unknownEventCount += 1;
        this.callbacks.onProtocolError(new Error(`引擎未知事件：${name}（已拒绝并计数）`));
        return;
      }
      this.callbacks.onEvent(line as unknown as EngineEvent);
      return;
    }
    // 响应行：含 id，且 result 或 error
    if (typeof line.id === "number") {
      const id = line.id;
      const entry = this.pending.get(id);
      if (!entry) return; // 无 pending（可能已超时）
      this.pending.delete(id);
      clearTimeout(entry.timer);
      if (line.error != null) {
        const err = line.error as { code?: number; message?: string };
        entry.reject(new Error(`引擎错误 ${err.code ?? "?"}：${err.message ?? "未知"}`));
      } else {
        entry.resolve(line.result);
      }
    }
  }

  private attachStderr(): void {
    const lines = createInterface({ input: this.child.stderr });
    lines.on("line", (raw) => {
      if (this.closed) return;
      this.callbacks.onStderr(maskText(raw));
    });
  }
}
