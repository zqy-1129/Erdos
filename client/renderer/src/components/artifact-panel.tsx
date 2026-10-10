import { useEffect, useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS, type ErdosBridge } from "../bridges/bridge.ts";
import type { ArtifactPreview, ArtifactView } from "../../../shared/artifact.ts";
import { useRemoteData } from "./use-remote.ts";
export function ArtifactPanel(props: { bridge: ErdosBridge; taskId: string; revision: number }): ReactNode {
  const remote = useRemoteData<ArtifactView[]>(() => props.bridge.invoke(BRIDGE_CHANNELS.artifactsList, { taskId: props.taskId }), [props.taskId, props.revision]);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setPreview(null); }, [props.taskId]);
  const act = async (entry: ArtifactView, save: boolean) => {
    setBusy(true); setError(null);
    try {
      if (save) await props.bridge.invoke(BRIDGE_CHANNELS.artifactsSave, { taskId: props.taskId, id: entry.id });
      else setPreview(await props.bridge.invoke<ArtifactPreview>(BRIDGE_CHANNELS.artifactsPreview, { taskId: props.taskId, id: entry.id }));
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  return <div className="form-block"><h3>任务产物</h3>
    {remote.error || error ? <p role="alert" className="form-error">{error ?? remote.error}</p> : null}
    <button type="button" onClick={remote.reload} disabled={busy}>刷新产物</button>
    {remote.data?.map(entry => <p key={entry.id}>{entry.stage} · {entry.name} · {entry.size} 字节
      <button type="button" disabled={busy} onClick={() => void act(entry, false)}>预览</button>
      <button type="button" disabled={busy} onClick={() => void act(entry, true)}>导出</button>
    </p>)}
    {preview?.kind === "text" ? <pre style={{ whiteSpace: "pre-wrap" }}>{preview.text}</pre> : null}
    {preview?.kind === "image" ? <img src={preview.imageUrl} alt={preview.name} style={{ maxWidth: "100%" }} /> : null}
    {preview?.kind === "binary" ? <p>此格式请导出后查看。</p> : null}
  </div>;
}
