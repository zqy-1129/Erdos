/**
 * 设置页（SP3-4）：偏好（默认模型/界面语言）、数据目录（只读）、版本、登出。
 * 登出即回登录页；错误以可读形式展示。
 */

import { useEffect, useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS } from "../bridges/bridge.ts";
import { useRemoteData } from "../components/use-remote.ts";
import type { ClientPreferences } from "../../../shared/preferences.ts";
import { navigate } from "../router.tsx";
import { logoutAction, type AppStores } from "../state/app-stores.ts";
import { useStore } from "../storage/store.ts";
import { UpdatePanel } from "../components/update-panel.tsx";

export function SettingsPage(props: { stores: AppStores }): ReactNode {
  const session = useStore(props.stores.session);
  const remote = useRemoteData<ClientPreferences>(() => props.stores.bridge.invoke(BRIDGE_CHANNELS.preferencesGet));
  const [model, setModel] = useState("");
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (remote.data) setModel(remote.data.defaultModel); }, [remote.data]);
  const save = async () => {
    setSaving(true); setSaved(false); setError(null);
    try {
      await props.stores.bridge.invoke(BRIDGE_CHANNELS.preferencesSave, { defaultModel: model, language: "zh-CN" });
      setSaved(true);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSaving(false); }
  };

  return (
    <div className="page">
      <h2>设置</h2>
      <UpdatePanel bridge={props.stores.bridge} />
      <div className="form-block">
        <label>
          默认模型
          <input value={model} maxLength={128} placeholder="留空使用 Key 管理中的模型" onChange={(e) => { setModel(e.target.value); setSaved(false); }} />
        </label>
        <label>
          界面语言
          <select value="zh-CN" disabled>
            <option value="zh-CN">简体中文</option>
          </select>
        </label>
        <div>
          <button type="button" className="btn btn-primary" disabled={saving} onClick={() => void save()}>
            保存偏好
          </button>
          {saved ? <span className="ok-text">已保存</span> : null}
          {error || remote.error ? <p className="form-error" role="alert">{error ?? remote.error}</p> : null}
        </div>
      </div>

      <div className="form-block">
        <h3>关于</h3>
        <p className="muted">本地留痕与加密凭据保存在系统应用数据目录。</p>
        <p className="muted" data-testid="boot-metrics">
          冷启动耗时：{(globalThis as { __erdosBoot?: { appReadyMs(): number | null } }).__erdosBoot?.appReadyMs() ?? "—"} ms（目标 &lt; 3000ms）
        </p>
        {(() => {
          const guard = (globalThis as { __erdosCrash?: { stats(): { errors: number; rejections: number } } }).__erdosCrash;
          return guard ? (
            <p className="muted" data-testid="crash-metrics">
              崩溃基线：异常 {guard.stats().errors} 次 / Promise 拒绝 {guard.stats().rejections} 次（目标崩溃率 &lt; 0.5%）
            </p>
          ) : null;
        })()}
      </div>

      <div className="form-block">
        <h3>账号</h3>
        <p className="muted">当前登录：{session.username}</p>
        <button
          type="button"
          className="btn"
          onClick={() => {
            logoutAction(props.stores);
            navigate("/");
          }}
        >
          退出登录
        </button>
      </div>
    </div>
  );
}
