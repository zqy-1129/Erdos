/**
 * 三态占位组件（SP3-4）：空态 / 异常态 / 断网态。
 * 执行计划验收：每个页面含空态/异常态/断网态三态。
 */

import type { ReactNode } from "react";

export function EmptyState(props: { title: string; hint?: string; action?: ReactNode }): ReactNode {
  return (
    <div className="state-card">
      <div className="state-icon">◌</div>
      <div className="state-title">{props.title}</div>
      {props.hint ? <div className="state-hint">{props.hint}</div> : null}
      {props.action ? <div className="state-action">{props.action}</div> : null}
    </div>
  );
}

export function ErrorState(props: { title: string; detail?: string; onRetry?: () => void }): ReactNode {
  return (
    <div className="state-card state-error">
      <div className="state-icon">⚠</div>
      <div className="state-title">{props.title}</div>
      {props.detail ? <div className="state-hint">{props.detail}</div> : null}
      {props.onRetry ? (
        <div className="state-action">
          <button type="button" className="btn" onClick={props.onRetry}>
            重试
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function OfflineState(props: { title?: string; hint?: string }): ReactNode {
  return (
    <div className="state-card state-offline">
      <div className="state-icon">⛔</div>
      <div className="state-title">{props.title ?? "网络不可用"}</div>
      <div className="state-hint">{props.hint ?? "已进入断网模式：本地功能可用，云端数据暂不可达，联网后自动恢复。"}</div>
    </div>
  );
}

/** 加载占位。 */
export function LoadingState(props: { label?: string }): ReactNode {
  return (
    <div className="state-card">
      <div className="state-icon">⋯</div>
      <div className="state-title">{props.label ?? "加载中…"}</div>
    </div>
  );
}