/**
 * 登录页（SP3-4 / US-001 注册体验）：登录与注册（注册即登录）双动作，
 * 全部经桥（auth:login / auth:register）；错误态展示可读原因。
 */
/// <reference types="vite/client" />

import { useState, type FormEvent, type ReactNode } from "react";
import { loginAction, registerAction, type AppStores } from "../state/app-stores.ts";
import { useStore } from "../storage/store.ts";
import { PasswordResetPanel } from "../components/password-reset-panel.tsx";

export function LoginPage(props: { stores: AppStores }): ReactNode {
  const session = useStore(props.stores.session);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (mode: "login" | "register") => {
    setBusy(true);
    try {
      if (mode === "register") {
        await registerAction(props.stores, username, password);
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
        <PasswordResetPanel bridge={props.stores.bridge} />
        {/* 禁用态原因可见（试用反馈：注册按钮灰置无提示，用户不知因密码不足 8 位） */}
        {username !== "" && password !== "" && password.length < 8 ? (
          <p className="login-hint">注册要求：密码至少 8 位，且同时包含字母与数字（例如 Test1234）。</p>
        ) : null}
        {import.meta.env.DEV ? (
          <p className="login-hint">
            联调提示：账号 demo-empty 演示空态、demo-error 演示异常态（开发模式）。
          </p>
        ) : null}
      </form>
    </div>
  );
}
