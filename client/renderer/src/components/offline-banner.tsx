/**
 * 断网态横幅（SP3-4 / US-006）：断网宽限倒计时 + 快照余额；
 * 超 72h 明确提示（非静默失败）；冻结/同步失败分级提示。
 */

import { useEffect, useState, type ReactNode } from "react";
import type { Store } from "../storage/store.ts";
import { useStore } from "../storage/store.ts";
import type { ConnectivityState, EntitlementState } from "../state/app-stores.ts";

function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

export function useTick(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function OfflineBanner(props: {
  connectivity: Store<ConnectivityState>;
  entitlement: Store<EntitlementState>;
}): ReactNode {
  const connectivity = useStore(props.connectivity);
  const entitlement = useStore(props.entitlement);
  const now = useTick(1000);

  let tone = "info";
  let text: string | null = null;
  if (!connectivity.online) {
    const deadline = entitlement.graceDeadlineMs;
    // 断网 ≠ 可用：宽限已超/冻结属危险态，离线时同样按 danger 提示（不得被「已断网」轻提示吞没）；
    // 无快照（未同步）不得展示「余额 0」造成已同步的假象
    if (entitlement.status === "grace_expired") {
      tone = "danger";
      text = "已断网：离线宽限已超 72 小时，请联网续期后继续（本次会话不会被静默丢弃）。";
    } else if (entitlement.status === "frozen") {
      tone = "danger";
      text = "已断网：账号欠费已冻结，请联网充值续费。";
    } else if (entitlement.status === "empty" || deadline === null) {
      tone = "info";
      text = "已断网：暂无本地权益快照（尚未同步）；联网后恢复余额与宽限展示。";
    } else {
      tone = "warn";
      text = `已断网（本地模式可用）：快照余额 ${entitlement.balance} 分，离线宽限剩余 ${formatRemaining(deadline - now)}`;
    }
  } else if (entitlement.status === "grace_expired") {
    tone = "danger";
    text = "离线宽限已超 72 小时：请联网续期后继续（本次会话不会被静默丢弃）。";
  } else if (entitlement.status === "frozen") {
    tone = "danger";
    text = "账号欠费已冻结：新阶段不可启动，请充值续费。";
  } else if (entitlement.stale) {
    tone = "info";
    text = "权益信息同步失败：当前展示上次快照数据。";
  }

  if (text === null) return null;
  return (
    <div className={`offline-banner tone-${tone}`} role="status">
      {text}
    </div>
  );
}