import { useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS, type ErdosBridge } from "../bridges/bridge.ts";
export function PasswordResetPanel(props: { bridge: ErdosBridge }): ReactNode {
  const [open, setOpen] = useState(false); const [identifier, setIdentifier] = useState("");
  const [token, setToken] = useState(""); const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = async (confirm: boolean) => {
    setBusy(true); setError(null); setMessage("");
    try {
      await props.bridge.invoke(confirm ? BRIDGE_CHANNELS.authResetConfirm : BRIDGE_CHANNELS.authResetRequest,
        confirm ? { token, password } : { identifier });
      setMessage(confirm ? "密码已重置，请使用新密码登录。" : "请求已受理；如账号存在，请从绑定渠道获取重置令牌。");
      if (confirm) { setToken(""); setPassword(""); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  return <div><button type="button" className="btn" disabled={busy} onClick={() => setOpen(!open)}>忘记密码</button>
    {open ? <div>
      <label>重置账号<input value={identifier} maxLength={255} onChange={e => setIdentifier(e.target.value)} /></label>
      <button type="button" disabled={busy || identifier.trim().length < 3} onClick={() => void send(false)}>发送重置请求</button>
      <label>重置令牌<input type="password" autoComplete="off" value={token} maxLength={128} onChange={e => setToken(e.target.value)} /></label>
      <label>新密码<input type="password" autoComplete="new-password" value={password} maxLength={128} onChange={e => setPassword(e.target.value)} /></label>
      <button type="button" disabled={busy || token.length < 16 || password.length < 8} onClick={() => void send(true)}>确认重置密码</button>
      {message ? <p role="status">{message}</p> : null}{error ? <p role="alert" className="form-error">{error}</p> : null}
    </div> : null}
  </div>;
}
