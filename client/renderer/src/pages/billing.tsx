/**
 * 账单页（SP3-4 / US-009）：订阅到期日（到期前 3 天提醒）、积分余额、
 * 积分流水（虚拟列表长表）+ 导出动作。三态齐备。
 */

import { useState, type ReactNode } from "react";
import {
  BRIDGE_CHANNELS,
  type BillingExportView,
  type BillingLedgerRow,
  type BillingOverview,
} from "../bridges/bridge.ts";
import { EmptyState, ErrorState, OfflineState } from "../components/states.tsx";
import { useRemoteData } from "../components/use-remote.ts";
import { VirtualList } from "../components/virtual-list.tsx";
import { useStore } from "../storage/store.ts";
import type { AppStores } from "../state/app-stores.ts";
import { CheckoutPanel } from "../components/checkout-panel.tsx";

/** 流水动作标签（对齐服务端 LedgerKind；未知类型回退原文，不猜测语义）。 */
function actionLabel(action: string): string {
  const labels: Record<string, string> = {
    grant: "赠分",
    reserve: "预扣",
    confirm: "消耗",
    refund: "退还",
    offline_sync: "离线补扣",
  };
  return labels[action] ?? action;
}

function renewNotice(subEndAt: string | null): string | null {
  if (!subEndAt) return null;
  const remainMs = Date.parse(subEndAt) - Date.now();
  if (remainMs >= 0 && remainMs <= 3 * 24 * 3600 * 1000) {
    return `订阅将于 ${Math.ceil(remainMs / (24 * 3600 * 1000))} 天后到期，请及时续费。`;
  }
  return null;
}

export function BillingPage(props: { stores: AppStores }): ReactNode {
  const connectivity = useStore(props.stores.connectivity);
  const overview = useRemoteData<BillingOverview>(() =>
    props.stores.bridge.invoke<BillingOverview>(BRIDGE_CHANNELS.billingOverview),
  );
  const ledger = useRemoteData<BillingLedgerRow[]>(() =>
    props.stores.bridge.invoke<BillingLedgerRow[]>(BRIDGE_CHANNELS.billingLedger),
  );
  const [exported, setExported] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  if (overview.error) {
    return (
      <div className="page">
        <h2>账单</h2>
        {overview.errorKind === "network" && !connectivity.online ? (
          <OfflineState />
        ) : (
          <ErrorState title="账单加载失败" detail={overview.error} onRetry={overview.reload} />
        )}
      </div>
    );
  }
  if (overview.loading || overview.data === null) {
    return <div className="page"><h2>账单</h2><div className="state-card">加载中…</div></div>;
  }

  const notice = renewNotice(overview.data.subEndAt);
  const ledgerRows = ledger.data ?? [];
  const ledgerEmpty = ledger.loading ? null : ledger.error ? "ledger-error" : ledgerRows.length === 0 ? "empty" : null;

  const doExport = async () => {
    // 新一轮导出先清上次结果：成功/错误/取消文案互斥，不残留（并防连点开多个保存对话框）
    setExported(null);
    setExportError(null);
    setExporting(true);
    try {
      const result = await props.stores.bridge.invoke<BillingExportView>(BRIDGE_CHANNELS.billingExport);
      if (result.canceled) return; // 用户在保存对话框中取消：静默（不提示失败）
      setExported(result.savedPath ? `已保存至 ${result.savedPath}` : `已生成 ${result.filename}`);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : String(error));
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="page">
      <h2>账单</h2>
      <CheckoutPanel bridge={props.stores.bridge} onPaid={() => { overview.reload(); ledger.reload(); }} />
      {notice ? <div className="renew-notice">{notice}</div> : null}
      <div className="kpis">
        <div className="kpi">
          <span className="kpi-label">套餐</span>
          <span className="kpi-value">{overview.data.planName}</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">订阅到期日</span>
          <span className="kpi-value">{overview.data.subEndAt ? overview.data.subEndAt.slice(0, 10) : "—"}</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">积分余额</span>
          <span className="kpi-value">{overview.data.pointsBalance}</span>
        </div>
      </div>

      <div className="ledger-head">
        <h3>积分流水</h3>
        <div>
          <button type="button" className="btn" disabled={exporting} onClick={() => void doExport()}>
            导出流水（CSV）
          </button>
          {exported ? <span className="ok-text">{exported}</span> : null}
          {exportError ? <span className="form-error">{exportError}</span> : null}
        </div>
      </div>
      {ledgerEmpty === "empty" ? (
        <EmptyState title="暂无流水" hint="注册赠分与阶段消耗记录会出现在这里。" />
      ) : ledgerEmpty === "ledger-error" ? (
        <ErrorState title="流水加载失败" detail={ledger.error ?? ""} onRetry={ledger.reload} />
      ) : (
        <VirtualList
          items={ledgerRows}
          rowHeight={32}
          height={320}
          renderRow={(row) => (
            <div className="ledger-row">
              <span className="mono">{row.ts.replace("T", " ").replace("Z", "")}</span>
              <span>{row.stage}</span>
              <span>{actionLabel(row.action)}</span>
              <span className={row.points >= 0 ? "pos" : "neg"}>{row.refundedPoints !== undefined ? `退还 ${row.refundedPoints}（净消耗 0）` : row.points >= 0 ? `+${row.points}` : row.points}</span>
              <span className="mono">{row.taskId}</span>
            </div>
          )}
        />
      )}
    </div>
  );
}
