import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ErdosBridge } from "../bridges/bridge.ts";
import { BRIDGE_CHANNELS } from "../bridges/bridge.ts";
import type { ImportProgress, ImportedProblem } from "../../../shared/problem-import.ts";

const PHASE_LABELS = { selecting: "请选择题面文件", reading: "正在读取文件", extracting: "正在提取题面文字",
  complete: "题面导入完成", cancelled: "题面导入已取消", failed: "题面导入失败", timed_out: "题面解析超时" };

/** 导入操作与轮询同一标识绑定；离开页面会取消，迟到响应不能修改其他任务的草稿。 */
export function ProblemImportPanel(props: { bridge: ErdosBridge; disabled: boolean;
  onImported: (problem: ImportedProblem) => void; onBusyChange: (busy: boolean) => void }): ReactNode {
  const active = useRef<{ id: string; disposed: boolean; polling: boolean } | null>(null);
  const [working, setWorking] = useState(false);
  const [progress, setProgress] = useState<ImportProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => () => {
    const operation = active.current;
    if (operation) {
      operation.disposed = true;
      void props.bridge.invoke(BRIDGE_CHANNELS.taskImportCancel, { importId: operation.id }).catch(() => {});
    }
    props.onBusyChange(false);
  }, [props.bridge]);

  const start = async () => {
    if (active.current || props.disabled) return;
    const operation = { id: crypto.randomUUID(), disposed: false, polling: false };
    active.current = operation; setWorking(true); setError(null); props.onBusyChange(true);
    setProgress({ importId: operation.id, phase: "selecting", progress: null });
    const poll = async () => {
      if (operation.disposed || operation.polling) return;
      operation.polling = true;
      try {
        const view = await props.bridge.invoke<ImportProgress | null>(BRIDGE_CHANNELS.taskImportStatus, { importId: operation.id });
        if (!operation.disposed && view?.importId === operation.id) setProgress(view);
      } catch { /* 进度请求不影响正在处理的导入；处理 RPC 负责返回最终错误。 */ }
      finally { operation.polling = false; }
    };
    const timer = setInterval(() => { void poll(); }, 250);
    try {
      const result = await props.bridge.invoke<ImportedProblem | null>(BRIDGE_CHANNELS.taskImport, { importId: operation.id });
      if (!operation.disposed) {
        if (result) props.onImported(result);
        setError(null);
        setProgress({ importId: operation.id, phase: result ? "complete" : "cancelled", progress: result ? 1 : null });
      }
    } catch (cause) {
      if (!operation.disposed) {
        const message = cause instanceof Error ? cause.message : String(cause);
        setError(message); setProgress({ importId: operation.id, phase: /超时/.test(message) ? "timed_out" : "failed", progress: null });
      }
    } finally {
      clearInterval(timer); operation.disposed = true;
      if (active.current === operation) {
        active.current = null;
        setWorking(false); props.onBusyChange(false);
      }
    }
  };
  const cancel = async () => {
    const operation = active.current;
    if (!operation) return;
    try { await props.bridge.invoke(BRIDGE_CHANNELS.taskImportCancel, { importId: operation.id }); }
    catch { if (!operation.disposed) setError("取消导入失败，请重试或等待处理结束"); }
  };
  return <div className="import-panel" aria-busy={working}>
    <button type="button" className="btn" disabled={props.disabled || working} onClick={() => void start()}>导入题面文件</button>
    {working ? <button type="button" className="btn" disabled={progress?.phase === "selecting"} onClick={() => void cancel()}>取消导入</button> : null}
    {progress ? <p role="status" aria-live="polite">{PHASE_LABELS[progress.phase]}
      {working && progress.progress !== null ? ` · ${Math.round(progress.progress * 100)}%` : ""}</p> : null}
    {working ? <progress aria-label="题面导入进度" max={100} value={progress?.progress === null ? undefined : (progress?.progress ?? 0) * 100} /> : null}
    {error ? <p className="form-error" role="alert">{error}</p> : null}
  </div>;
}
