/**
 * EngineHost 状态机（FE-HOST W3）：懒启动 / spawn / 看门狗 / 单实例语义 / 退出链。
 *
 * 状态机（客户端开发详细方案 §3.2）：
 *   idle → spawning（懒启动）→ ready（首行写入后首个 RPC 往返成功）
 *   ready ⇄ running（任务执行中 / 阶段终态）
 *   spawning|ready|running → crashed（进程退出，非用户停止）→ spawning（重启 ≤3 次，退避 1s/2s/4s）→ failed（耗尽）
 *   ready → stopped（用户退出 / 空闲 10min 无 in-flight）
 *
 * 关键判定（防误杀）：
 * - 看门狗只监听进程退出事件与退出码，不以 stdout 静默时长判崩溃（模型长调用期间 stdout 可能静默数分钟）；
 * - 崩溃重启不重放 in-flight 请求：crashed 前 pending 一律以 ENGINE_RESTART 失败，由 UI 引导重试/恢复。
 */
import { spawn, exec } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { EngineRpcClient } from "./rpc.ts";
import { injectFirstLine, INJECT_READY_TIMEOUT_MS } from "./inject.ts";
import type { EngineEvent, InitializeResult, RpcMethod } from "../../shared/ipc.ts";

export type EngineHostState =
  | "idle"
  | "spawning"
  | "ready"
  | "running"
  | "crashed"
  | "failed"
  | "stopped";

const MAX_RESTARTS = 3;
const RESTART_BACKOFF_MS = [1000, 2000, 4000];
const IDLE_TIMEOUT_MS = 10 * 60 * 1000; // 空闲回收（客户端架构 §5.1）
const SIGTERM_GRACE_MS = 300; // 优雅退出宽限（engine-protocol.md §4）
const RELOAD_EXIT_TIMEOUT_MS = 2000; // 密钥重载：等待旧进程退出的上限

export interface EngineHostOptions {
  /** 引擎可执行（开发期指向 engine/.venv 的 python；打包后指向 PyInstaller 产物）。 */
  command: string;
  args: string[];
  /** 引擎进程工作目录（`python -m engine` 需能定位 engine 包，指向其父目录）。 */
  cwd?: string;
  /** ERDOS_ENGINE_HOME（任务/库数据根目录）。 */
  home: string;
  /** 懒加载 Key（spawn 时调用一次，写毕即弃）。 */
  key: () => string | null;
  onEvent: (event: EngineEvent) => void;
  onStateChange: (state: EngineHostState) => void;
  /** 已脱敏的引擎 stderr 单行。 */
  onLog: (line: string) => void;
  onProtocolError: (err: Error) => void;
}

export class EngineHost {
  private readonly options: EngineHostOptions;
  private state: EngineHostState = "idle";
  private child: ChildProcessWithoutNullStreams | null = null;
  private rpc: EngineRpcClient | null = null;
  private restartCount = 0;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private userStopping = false;

  constructor(options: EngineHostOptions) {
    this.options = options;
  }

  get currentState(): EngineHostState {
    return this.state;
  }

  /** 当前引擎子进程 pid（诊断/看门狗测试用；未 spawn 或已退出时 null）。 */
  get childPid(): number | null {
    return this.child?.pid ?? null;
  }

  /** 懒启动：首次任务或用户显式触发才 spawn（冷启动不含引擎拉起）。 */
  ensureStarted(): void {
    if (this.state === "idle" || this.state === "failed") {
      this.transition("spawning");
      this.spawn();
    }
  }

  /** 转发 RPC 到引擎（若未启动则懒启动后等待就绪）。 */
  async invoke<T = unknown>(method: RpcMethod, params: Record<string, unknown>): Promise<T> {
    this.ensureStarted();
    const rpc = await this.awaitReady();
    if (method === "start_stage") {
      this.transition("running");
    }
    this.resetIdleTimer();
    return rpc.invoke<T>(method, params);
  }

  /**
   * 重载密钥（FE-KEYIN W5 运行期语义）：Key 新增/切换后重启引擎，
   * 使下一次 spawn 经 key() 重新注入最新明文（首行注入只在 spawn 时发生）。
   *
   * 语义：
   * - idle（从未启动）→ 直接懒启动（新 Key 自然注入）；
   * - stopped/crashed/failed → 复位用户停止标记后重启；
   * - ready/running → 优雅退出 → 等待旧进程退出（≤2s）→ 重启；
   * - 返回时保证引擎已就绪（失败抛错，由调用方按 probe 失败呈现）。
   */
  async reloadKey(): Promise<void> {
    if (this.state === "idle") {
      this.ensureStarted();
      await this.awaitReady();
      return;
    }
    if (this.state === "stopped" || this.state === "crashed" || this.state === "failed") {
      this.userStopping = false;
      this.transition("spawning");
      this.spawn();
      await this.awaitReady();
      return;
    }
    this.stop();
    await this.waitExit(RELOAD_EXIT_TIMEOUT_MS);
    this.userStopping = false; // 复位：旧进程退出已按用户停止处理，新进程参与看门狗
    this.transition("spawning");
    this.spawn();
    await this.awaitReady();
  }

  /** 等待当前子进程退出（轮询；超时返回，交由 SIGTERM/taskkill 兜底回收）。 */
  private waitExit(timeoutMs: number): Promise<void> {
    return new Promise((resolve) => {
      const start = Date.now();
      const poll = (): void => {
        if (this.child === null || this.childPid === null || Date.now() - start > timeoutMs) {
          resolve();
          return;
        }
        setTimeout(poll, 25);
      };
      poll();
    });
  }

  /** 优雅退出：协作取消 → SIGTERM → 300ms → taskkill 兜底 → 校验无遗留。 */
  stop(): void {
    if (this.state === "stopped" || this.state === "idle") return;
    this.userStopping = true;
    this.clearIdleTimer();
    if (this.restartTimer !== null) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (this.rpc && this.state === "ready") {
      // 协作取消当前阶段（best-effort，不等待）
      void this.rpc.invoke("cancel", {}).catch(() => {});
    }
    if (this.child) {
      const child = this.child;
      const grace = setTimeout(() => {
        // SIGTERM 后仍未退出：taskkill /PID /T /F 兜底（Windows 无遗留进程）
        if (child.pid != null) {
          exec(`taskkill /PID ${child.pid} /T /F`, () => {});
        }
      }, SIGTERM_GRACE_MS);
      child.once("exit", () => clearTimeout(grace));
      child.kill("SIGTERM");
    }
    this.rpc?.close();
    this.transition("stopped");
  }

  private spawn(): void {
    const { command, args, cwd, home, key } = this.options;
    const child = spawn(command, args, {
      cwd,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, ERDOS_ENGINE_HOME: home },
    }) as ChildProcessWithoutNullStreams;
    this.child = child;

    const rpc = new EngineRpcClient(child, {
      onEvent: (event) => {
        if (isCurrent()) this.onEngineEvent(event);
      },
      onStderr: (line) => this.options.onLog(line),
      onProtocolError: (err) => this.options.onProtocolError(err),
    });
    this.rpc = rpc;
    /**
     * 迟到事件防护：reloadKey/看门狗重启后，旧进程的 exit/error/就绪回调不得
     * 干扰新进程（否则会误置 crashed 或清空新 rpc 引用）。
     */
    const isCurrent = (): boolean => this.rpc === rpc;

    // 首行注入（Key 或空行），写毕即弃引用
    const injected = key();
    injectFirstLine(child, injected);

    // 就绪判定：initialize（CT-V2）成功 → ready；失败回退 get_status；超时 crashed
    const readyTimer = setTimeout(() => {
      if (isCurrent()) this.onSpawnTimeout();
    }, INJECT_READY_TIMEOUT_MS);
    void rpc
      .invoke<InitializeResult>("initialize", { client_protocol_version: 2 }, INJECT_READY_TIMEOUT_MS - 500)
      .then((res) => {
        if (!isCurrent()) return;
        clearTimeout(readyTimer);
        if (res && res.compatible === false) {
          this.onSpawnFailed(new Error(`协议版本不兼容（引擎 ${res.engine_version}）`));
          return;
        }
        this.restartCount = 0;
        this.transition("ready");
        this.resetIdleTimer();
      })
      .catch(() => {
        if (!isCurrent()) return;
        // initialize 不可用（旧引擎）：回退 get_status 判活
        rpc
          .invoke("get_status", {}, INJECT_READY_TIMEOUT_MS - 500)
          .then(() => {
            if (!isCurrent()) return;
            clearTimeout(readyTimer);
            this.restartCount = 0;
            this.transition("ready");
            this.resetIdleTimer();
          })
          .catch(() => {
            if (isCurrent()) this.onSpawnTimeout();
          });
      });

    child.on("error", (err) => {
      if (isCurrent()) this.onSpawnFailed(err);
    });
    child.on("exit", (code, signal) => {
      if (isCurrent()) this.onExit(code, signal);
    });
  }

  private onEngineEvent(event: EngineEvent): void {
    // 阶段完成（progress>=1）或报告阶段产物：running → ready（恢复空闲回收计时）
    if (this.state === "running") {
      const isStageDone = event.event === "stage.progress" && event.progress >= 1;
      const isPaperReady = event.event === "artifact.ready" && event.artifact === "paper";
      if (isStageDone || isPaperReady) {
        this.transition("ready");
        this.resetIdleTimer();
      }
    }
    this.options.onEvent(event);
  }

  private awaitReady(): Promise<EngineRpcClient> {
    if (this.state === "ready" || this.state === "running") {
      return Promise.resolve(this.rpc as EngineRpcClient);
    }
    // spawning：轮询等待就绪（或 crashed/failed 快速失败）
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const poll = (): void => {
        if (this.state === "ready" || this.state === "running") {
          resolve(this.rpc as EngineRpcClient);
          return;
        }
        if (this.state === "crashed" || this.state === "failed" || this.state === "stopped") {
          reject(new Error("ENGINE_RESTART：引擎不可用"));
          return;
        }
        if (Date.now() - start > INJECT_READY_TIMEOUT_MS) {
          reject(new Error("引擎启动超时"));
          return;
        }
        setTimeout(poll, 50);
      };
      poll();
    });
  }

  private onSpawnFailed(err: Error): void {
    this.rpc?.close();
    this.transition("crashed");
    this.scheduleRestart(err);
  }

  private onSpawnTimeout(): void {
    this.rpc?.close();
    // 超时但进程可能仍存活：强制回收，避免泄漏后再 spawn 出双进程
    if (this.child) {
      const child = this.child;
      child.kill("SIGTERM");
      if (child.pid != null) {
        exec(`taskkill /PID ${child.pid} /T /F`, () => {});
      }
    }
    this.transition("crashed");
    this.scheduleRestart(new Error("引擎启动超时 10s"));
  }

  private onExit(code: number | null, signal: NodeJS.Signals | null): void {
    this.rpc?.close();
    this.rpc = null;
    this.child = null;
    if (this.userStopping) return; // 用户主动停止：不重启
    this.transition("crashed");
    this.scheduleRestart(new Error(`引擎进程退出（code=${code ?? "?"} signal=${signal ?? "?"}）`));
  }

  private scheduleRestart(cause: Error): void {
    if (this.restartCount >= MAX_RESTARTS) {
      this.transition("failed");
      this.options.onLog(`[engine-host] 看门狗重试耗尽（${MAX_RESTARTS} 次）：${cause.message}`);
      return;
    }
    const delay = RESTART_BACKOFF_MS[this.restartCount] ?? 4000;
    this.restartCount += 1;
    this.options.onLog(`[engine-host] 看门狗第 ${this.restartCount}/${MAX_RESTARTS} 次重启（${delay}ms 后）：${cause.message}`);
    this.restartTimer = setTimeout(() => {
      if (this.userStopping || this.state === "stopped") return;
      this.transition("spawning");
      this.spawn();
    }, delay);
  }

  private resetIdleTimer(): void {
    this.clearIdleTimer();
    this.idleTimer = setTimeout(() => {
      if (this.state === "ready") {
        this.stop();
      }
    }, IDLE_TIMEOUT_MS);
  }

  private clearIdleTimer(): void {
    if (this.idleTimer !== null) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  private transition(next: EngineHostState): void {
    if (this.state === next) return;
    this.state = next;
    this.options.onStateChange(next);
  }
}
