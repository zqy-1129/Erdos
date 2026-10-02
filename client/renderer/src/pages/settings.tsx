/**
 * 设置页（SP3-4）：偏好（默认模型/界面语言）、数据目录（只读）、版本、登出。
 * 登出即回登录页；错误以可读形式展示。
 */

import { useState, type ReactNode } from "react";
import { navigate } from "../router.tsx";
import { logoutAction, type AppStores } from "../state/app-stores.ts";
import { useStore } from "../storage/store.ts";

export function SettingsPage(props: { stores: AppStores }): ReactNode {
  const session = useStore(props.stores.session);
  const [model, setModel] = useState("deepseek-chat");
  const [lang, setLang] = useState("zh-CN");
  const [saved, setSaved] = useState(false);

  return (
    <div className="page">
      <h2>设置</h2>
      <div className="form-block">
        <label>
          默认模型
          <select value={model} onChange={(e) => { setModel(e.target.value); setSaved(false); }}>
            <option value="deepseek-chat">DeepSeek-V3（对话/推理均衡）</option>
            <option value="deepseek-reasoner">DeepSeek-R1（深度推理）</option>
          </select>
        </label>
        <label>
          界面语言
          <select value={lang} onChange={(e) => { setLang(e.target.value); setSaved(false); }}>
            <option value="zh-CN">简体中文</option>
            <option value="en">English</option>
          </select>
        </label>
        <div>
          <button type="button" className="btn btn-primary" onClick={() => setSaved(true)}>
            保存偏好
          </button>
          {saved ? <span className="ok-text">已保存</span> : null}
        </div>
      </div>

      <div className="form-block">
        <h3>关于</h3>
        <p className="muted">版本：0.1.0（开发模式）</p>
        <p className="muted">数据目录：%APPDATA%/erdos（本地留痕与密钥密文存储于此）</p>
        <p className="muted" data-testid="boot-metrics">
          冷启动耗时：{(globalThis as { __erdosBoot?: { appReadyMs(): number | null } }).__erdosBoot?.appReadyMs() ?? "—"} ms（目标 &lt; 3000ms）
        </p>
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