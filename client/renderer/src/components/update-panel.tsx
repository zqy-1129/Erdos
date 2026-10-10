import { useEffect, useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS, type ErdosBridge } from "../bridges/bridge.ts";
import type { UpdateView } from "../../../shared/update.ts";
export function UpdatePanel(props: { bridge: ErdosBridge }): ReactNode {
  const [view, setView] = useState<UpdateView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let disposed = false;
    const read = () => props.bridge.invoke<UpdateView>(BRIDGE_CHANNELS.updateStatus).then(next => {
      if (!disposed) setView(next);
    }).catch(cause => { if (!disposed) setError(String(cause)); });
    void read();
    const timer = setInterval(() => void read(), 2000);
    return () => { disposed = true; clearInterval(timer); };
  }, [props.bridge]);
  const act = async (channel: string) => {
    setBusy(true); setError(null);
    try { setView(await props.bridge.invoke<UpdateView>(channel)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  return <div className="form-block"><h3>应用更新</h3>
    {view ? <p>当前版本：{view.currentVersion} {view.version ? "→ " + view.version : ""} · {view.message}</p> : <p>正在读取更新状态…</p>}
    {view?.status === "downloading" ? <p role="status">下载进度：{Math.round(view.progress)}%</p> : null}
    <button type="button" className="btn" disabled={busy || !view || ["disabled", "checking", "downloading"].includes(view.status)}
      onClick={() => void act(BRIDGE_CHANNELS.updateCheck)}>检查更新</button>
    {view?.status === "available" ? <button type="button" className="btn" disabled={busy} onClick={() => void act(BRIDGE_CHANNELS.updateDownload)}>下载更新</button> : null}
    {view?.status === "downloaded" ? <button type="button" className="btn" disabled={busy} onClick={() => void act(BRIDGE_CHANNELS.updateInstall)}>安装并重启</button> : null}
    {error ? <p role="alert" className="form-error">{error}</p> : null}
  </div>;
}
