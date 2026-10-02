/**
 * 崩溃率守卫（SP3-7 非功能验收前置）：window error / unhandledrejection 捕获统计。
 *
 * - CrashGuard 附加全局错误监听并累计 stats（errors/rejections/lastAt）；
 * - onCrash 回调可注入（SP3-7 主进程接遥测 app_error 上报，PRD：崩溃率 <0.5%）；
 * - 事件源可注入（测试）；attach 返回卸载函数。
 */

export interface CrashStats {
  errors: number;
  rejections: number;
  lastAt: string | null;
}

export interface ErrorLikeEvent {
  message?: unknown;
  error?: unknown;
  reason?: unknown;
}

export interface EventSourceLike {
  addEventListener(type: string, handler: (event: ErrorLikeEvent) => void): void;
  removeEventListener(type: string, handler: (event: ErrorLikeEvent) => void): void;
}

export type CrashKind = "error" | "unhandledrejection";

export class CrashGuard {
  private errors = 0;
  private rejections = 0;
  private lastAt: string | null = null;
  private readonly callback?: (kind: CrashKind, detail: string) => void;
  private readonly source: EventSourceLike | null;
  private readonly now: () => number;

  constructor(
    callback?: (kind: CrashKind, detail: string) => void,
    source: EventSourceLike | null = null,
    now: () => number = () => Date.now(),
  ) {
    this.callback = callback;
    this.source = source;
    this.now = now;
  }

  private readonly onError = (kind: CrashKind, detail: string) => (this.callback ?? (() => {}))(kind, detail);
  private readonly handleError = (event: ErrorLikeEvent) => {
    this.errors += 1;
    this.lastAt = new Date(this.now()).toISOString();
    this.onError("error", String(event.error ?? event.message ?? "unknown"));
  };
  private readonly handleRejection = (event: ErrorLikeEvent) => {
    this.rejections += 1;
    this.lastAt = new Date(this.now()).toISOString();
    this.onError("unhandledrejection", String(event.reason ?? "unknown"));
  };

  /** 附加监听（source 缺省取 window）；返回卸载函数（幂等）。 */
  attach(): () => void {
    const source = this.source ?? ((typeof window !== "undefined" ? window : null) as EventSourceLike | null);
    if (!source) return () => {};
    source.addEventListener("error", this.handleError as (event: ErrorLikeEvent) => void);
    source.addEventListener("unhandledrejection", this.handleRejection as (event: ErrorLikeEvent) => void);
    let detached = false;
    return () => {
      if (detached) return;
      detached = true;
      source.removeEventListener("error", this.handleError as (event: ErrorLikeEvent) => void);
      source.removeEventListener("unhandledrejection", this.handleRejection as (event: ErrorLikeEvent) => void);
    };
  }

  /** 崩溃基线统计（PRD：崩溃率 <0.5%，基准 = 崩溃会话数/总启动）。 */
  stats(): CrashStats {
    return { errors: this.errors, rejections: this.rejections, lastAt: this.lastAt };
  }
}