/**
 * Key 管理（SP3-4 / US-002）：Key 列表（脱敏展示，明文永不出主进程）+
 * 连通测试向导（输入 → 加密保存 → 测试 → 分类可读结果）。
 * 三态：空态（无 Key）/ 异常态（列表加载失败）/ 断网态（banner + 页内 OfflineState）。
 */

import { useReducer, type ReactNode } from "react";
import {
  BRIDGE_CHANNELS,
  type KeyItemView,
  type KeyTestResult,
  type UsageEstimateView,
} from "../bridges/bridge.ts";
import { EmptyState, ErrorState, OfflineState } from "../components/states.tsx";
import { useRemoteData } from "../components/use-remote.ts";
import { useStore } from "../storage/store.ts";
import {
  initialWizard,
  keyWizardReducer,
  reasonLabel,
} from "../state/key-wizard.ts";
import type { AppStores } from "../state/app-stores.ts";

export function KeysPage(props: { stores: AppStores }): ReactNode {
  const connectivity = useStore(props.stores.connectivity);
  const remote = useRemoteData<KeyItemView[]>(() =>
    props.stores.bridge.invoke<KeyItemView[]>(BRIDGE_CHANNELS.keysList),
  );
  const usage = useRemoteData<UsageEstimateView>(() =>
    props.stores.bridge.invoke<UsageEstimateView>(BRIDGE_CHANNELS.keysUsage),
  );
  const [wizard, dispatch] = useReducer(keyWizardReducer, initialWizard);

  if (remote.loading) return <div className="page"><h2>Key 管理</h2><div className="state-card">加载中…</div></div>;
  if (remote.error) {
    return (
      <div className="page">
        <h2>Key 管理</h2>
        {remote.errorKind === "network" && !connectivity.online ? (
          <OfflineState />
        ) : (
          <ErrorState title="Key 列表加载失败" detail={remote.error} onRetry={remote.reload} />
        )}
      </div>
    );
  }

  const runSaveAndTest = async () => {
    dispatch({ type: "save" });
    try {
      await props.stores.bridge.invoke(BRIDGE_CHANNELS.keysSave, {
        alias: wizard.alias,
        baseUrl: wizard.baseUrl,
        key: wizard.key,
      });
      dispatch({ type: "saved" });
      const result = await props.stores.bridge.invoke<KeyTestResult>(BRIDGE_CHANNELS.keysTest, {
        baseUrl: wizard.baseUrl,
        key: wizard.key,
        model: wizard.model,
      });
      dispatch({ type: "test-result", result });
    } catch (error) {
      dispatch({ type: "fail", message: error instanceof Error ? error.message : String(error) });
    }
  };

  const runDelete = async (id: string) => {
    if (!window.confirm("确认删除该 Key？使用该 Key 的历史任务恢复时需重新配置。")) return;
    try {
      const result = await props.stores.bridge.invoke<{ ok: boolean; requeue: boolean }>(
        BRIDGE_CHANNELS.keysDelete,
        { id },
      );
      if (result.ok) {
        remote.reload();
        if (result.requeue) {
          window.alert("已删除当前激活 Key：恢复历史任务前请先重新配置 Key。");
        }
      }
    } catch (error) {
      window.alert(`删除失败：${error instanceof Error ? error.message : String(error)}`);
    }
  };

  return (
    <div className="page">
      <h2>Key 管理</h2>
      {usage.data ? (
        <div className="kpis">
          <div className="kpi">
            <span className="kpi-label">本地累计 Token（留痕）</span>
            <span className="kpi-value">{usage.data.totalTokens}</span>
          </div>
          <div className="kpi">
            <span className="kpi-label">模型调用次数</span>
            <span className="kpi-value">{usage.data.modelCalls}</span>
          </div>
          <div className="kpi">
            <span className="kpi-label">估算费用（内置单价）</span>
            <span className="kpi-value">
              {usage.data.estimatedCostCents === null
                ? "—（含未定价模型）"
                : `${usage.data.estimatedCostCents} 分`}
            </span>
          </div>
        </div>
      ) : null}
      {!connectivity.online ? <OfflineState hint="断网模式下仅展示本地已保存的 Key，暂不支持连通测试。" /> : null}

      {remote.data === null || remote.data.length === 0 ? (
        <EmptyState
          title="还没有配置 Key"
          hint="支持 OpenAI 兼容协议任意 Base URL；Key 加密存储，日志中不出现明文。"
          action={
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => document.getElementById("key-wizard")?.scrollIntoView()}
            >
              添加第一个 Key
            </button>
          }
        />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>别名</th>
              <th>Base URL</th>
              <th>Key（脱敏）</th>
              <th>连通状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {remote.data.map((item) => (
              <tr key={item.id}>
                <td>{item.alias}</td>
                <td>{item.baseUrl}</td>
                <td className="mono">{item.masked}</td>
                <td>{item.status === "ok" ? "✓ 正常" : item.status === "fail" ? "✗ 失败" : "未测试"}</td>
                <td>
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={() => void runDelete(item.id)}
                  >
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div id="key-wizard" className="wizard">
        <h3>连通测试向导</h3>
        {(wizard.stage === "input" || wizard.stage === "error") ? (
          <>
            {wizard.error ? <div className="form-error">{wizard.error}</div> : null}
            <label>
              别名
              <input
                value={wizard.alias}
                placeholder="如 DeepSeek"
                onChange={(e) => dispatch({ type: "field", field: "alias", value: e.target.value })}
              />
            </label>
            <label>
              Base URL（OpenAI 兼容）
              <input
                value={wizard.baseUrl}
                placeholder="https://api.deepseek.com/v1"
                onChange={(e) => dispatch({ type: "field", field: "baseUrl", value: e.target.value })}
              />
            </label>
            <label>
              API Key
              <input
                type="password"
                value={wizard.key}
                placeholder="sk-..."
                onChange={(e) => dispatch({ type: "field", field: "key", value: e.target.value })}
              />
            </label>
            <label>
              模型名（探测用，可选）
              <input
                value={wizard.model}
                placeholder="如 deepseek-chat"
                onChange={(e) => dispatch({ type: "field", field: "model", value: e.target.value })}
              />
            </label>
            <div>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!wizard.alias || !wizard.baseUrl || !wizard.key}
                onClick={() => void runSaveAndTest()}
              >
                保存并测试连通
              </button>
            </div>
          </>
        ) : null}
        {wizard.stage === "saving" || wizard.stage === "testing" ? (
          <div className="state-card">{wizard.stage === "saving" ? "正在加密保存…" : "正在连通测试…"}</div>
        ) : null}
        {wizard.stage === "done" && wizard.result ? (
          <div className={`wizard-result ${wizard.result.ok ? "ok" : "fail"}`} role="status">
            <b>{reasonLabel(wizard.result.reason)}</b>
            <p>{wizard.result.detail}</p>
            <button type="button" className="btn" onClick={() => dispatch({ type: "reset" })}>
              再测一个
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}