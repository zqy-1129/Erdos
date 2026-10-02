/**
 * 登录页（SP3-4 / US-001 注册体验）：登录与注册（注册即登录）双动作，
 * 全部经桥（auth:login / auth:register）；错误态展示可读原因。
 */

import { useState, type FormEvent, type ReactNode } from "react";
import { BRIDGE_CHANNELS } from "../bridges/bridge.ts";
import { loginAction, type AppStores } from "../state/app-stores.ts";
import { useStore } from "../storage/store.ts";

export function LoginPage(props: { stores: AppStores }): ReactNode {
  const session = useStore(props.stores.session);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (mode: "login" | "register") => {
    setBusy(true);
    try {
      if (mode === "register") {
        await props.stores.bridge.invoke(BRIDGE_CHANNELS.authRegister, { username, password });
        props.stores.session.setState({ status: "signed-in", username, error: null });
      } else {
        await loginAction(props.stores, username, password);
      }
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit("login");
  };

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>Erdos</h1>
        <p className="login-sub">数学建模竞赛智能助手 · 注册即赠 100 体验积分</p>
        <label>
          账号
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            placeholder="邮箱或手机号"
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            placeholder="至少 8 位"
          />
        </label>
        {session.error ? <div className="form-error">{session.error}</div> : null}
        <div className="login-actions">
          <button type="submit" className="btn btn-primary" disabled={busy || !username || !password}>
            登录
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy || !username || password.length < 8}
            onClick={() => void submit("register")}
          >
            注册并登录
          </button>
        </div>
        <p className="login-hint">
          联调提示：账号 demo-empty 演示空态、demo-error 演示异常态（开发模式）。
        </p>
      </form>
    </div>
  );
}