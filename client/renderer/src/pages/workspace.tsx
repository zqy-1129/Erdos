/** 工作台：登记真实题面，执行四阶段、审批与暂停恢复；所有操作经主进程许可入口。 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useStore } from "../storage/store.ts";
import { StageProgress, STAGE_LABELS, type AnswerGateParams } from "../components/stage-progress.tsx";
import { GatePanel } from "../components/gate-panel.tsx";
import { ArtifactPanel } from "../components/artifact-panel.tsx";
import { applyStatusSnapshot, initialEngineState, isGateConflict } from "../state/engine-slice.ts";
import { BRIDGE_CHANNELS } from "../bridges/bridge.ts";
import type { AppStores } from "../state/app-stores.ts";
import type { AnswerGateResult, GetStatusResult, StageName } from "../../../shared/ipc.ts";
import { STAGE_POINTS } from "../../../shared/product.ts";

const EXAMPLE = { title: "五点线性拟合", problem_text: "用最小二乘拟合 y=a*x+b。数据为 (0,2.1),(1,4.0),(2,6.2),(3,7.8),(4,10.1)。给出拟合参数、验证计算结果并形成简短报告。" };

export function WorkspacePage(props: { stores: AppStores }): ReactNode {
  const engine = useStore(props.stores.engine);
  const [title, setTitle] = useState("");
  const [problem, setProblem] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const draftId = useRef<string | null>(null);
  const [status, setStatus] = useState<GetStatusResult | null>(null);
  const [complete, setComplete] = useState(false);
  const [cancelled, setCancelled] = useState(false);
  useEffect(() => {
    if (props.stores.engine.getState().taskId) return;
    let disposed = false;
    void props.stores.bridge.invoke<{ taskId: string; stage: StageName } | null>(BRIDGE_CHANNELS.taskPending).then(pending => {
      if (!disposed && pending) props.stores.engine.setState({ taskId: pending.taskId, stage: pending.stage, running: false });
    }).catch(cause => { if (!disposed) setError(String(cause)); });
    return () => { disposed = true; };
  }, [props.stores]);
  const refresh = useCallback(async () => {
    const next = await props.stores.bridge.invoke<GetStatusResult>("engine:get_status", {});
    if (!next) return;
    setStatus(next);
    props.stores.engine.setState(applyStatusSnapshot(props.stores.engine.getState(), next));
    const taskId = next.task?.task_id ?? props.stores.engine.getState().taskId;
    if (taskId) {
      const task = await props.stores.bridge.invoke<{ completed: boolean }>(BRIDGE_CHANNELS.taskStatus, { taskId });
      if (task?.completed) setComplete(true);
    }
    if (next.task?.status === "failed") setError("阶段执行失败，预扣将退还。请查看错误后重试或取消任务。");
  }, [props.stores]);
  useEffect(() => {
    if (!engine.taskId || complete) return;
    const timer = setInterval(() => { if (!locked.current) void refresh().catch(cause => setError(String(cause))); }, 1000);
    return () => clearInterval(timer);
  }, [engine.taskId, complete, refresh]);
  const run = async (operation: () => Promise<void>) => {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError(null);
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { locked.current = false; setBusy(false); }
  };
  const start = async (input = { title, problem_text: problem }) => {
    if (!input.title.trim() || !input.problem_text.trim()) throw new Error("请填写任务标题和题面");
    draftId.current ??= crypto.randomUUID();
    const taskId = draftId.current;
    await props.stores.bridge.invoke("engine:task_create", { task_id: taskId, ...input });
    props.stores.engine.setState({ ...initialEngineState, taskId, stage: "analysis", running: false });
    await props.stores.bridge.invoke("engine:start_stage", { task_id: taskId, stage: "analysis" });
    props.stores.engine.setState({ taskId, stage: "analysis", running: true });
    setComplete(false);
    setCancelled(false);
    await refresh();
  };
  const answer = async (params: AnswerGateParams) => {
    if (locked.current) return;
    locked.current = true; setBusy(true);
    const stage = params.gate.replace(/^gate_/, "") as StageName;
    try {
      const result = await props.stores.bridge.invoke<AnswerGateResult>("engine:answer_gate", {
        task_id: params.taskId, gate: "gate_" + stage, decision: params.decision, feedback: params.feedback,
      });
      await refresh();
      props.stores.engine.setState({ gates: [] });
      if (result.action === "complete") { setComplete(true); return; }
      const next = result.action === "retry_stage" ? stage :
        (await props.stores.bridge.invoke<GetStatusResult>("engine:get_status", {})).orchestrator?.current_stage;
      if (next) {
        await props.stores.bridge.invoke("engine:start_stage", { task_id: params.taskId, stage: next });
        await refresh();
      }
    } catch (cause) {
      if (isGateConflict(cause)) await refresh();
      throw cause;
    } finally { locked.current = false; setBusy(false); }
  };
  const control = (method: "pause" | "resume" | "cancel") => run(async () => {
    await props.stores.bridge.invoke("engine:" + method, { task_id: engine.taskId });
    await refresh();
    if (method === "cancel") setCancelled(true);
  });
  return (
    <div className="page">
      <h2>工作台</h2>
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      {engine.taskId === null ? (
        <div className="form-block">
          <h3>从示例题开始，或导入自己的题面</h3>
          <p className="muted">各阶段使用你的模型 API；平台积分按分析 20、建模 30、求解 40、写作 30 计量，开始前预扣。</p>
          <label>任务标题<input maxLength={128} value={title} onChange={e => { setTitle(e.target.value); draftId.current = null; }} /></label>
          <label>题面<textarea rows={10} maxLength={60000} value={problem} onChange={e => { setProblem(e.target.value); draftId.current = null; }} /></label>
          <button type="button" className="btn" disabled={busy} onClick={() => void run(async () => {
            const imported = await props.stores.bridge.invoke<{ title: string; problemText: string } | null>(BRIDGE_CHANNELS.taskImport);
            if (imported) { setTitle(imported.title); setProblem(imported.problemText); draftId.current = null; }
          })}>导入题面文件</button>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void run(() => start())}>开始分析</button>
          <button type="button" className="btn" disabled={busy} onClick={() => void run(() => start(EXAMPLE))}>一键开始四阶段</button>
        </div>
      ) : (
        <>
          <div className="stage-toolbar">
            <button type="button" className="btn" disabled={busy || !engine.running} onClick={() => void control("pause")}>暂停</button>
            <button type="button" className="btn" disabled={busy || status?.task?.status !== "paused"} onClick={() => void control("resume")}>继续</button>
            <button type="button" className="btn btn-danger" disabled={busy || complete} onClick={() => void control("cancel")}>取消任务</button>
            {!cancelled && (status?.task?.status === "failed" || (!status?.task && !engine.running)) ? <button type="button" className="btn" disabled={busy} onClick={() => void run(async () => {
              await props.stores.bridge.invoke("engine:start_stage", { task_id: engine.taskId, stage: engine.stage }); await refresh();
            })}>重试本阶段</button> : null}
          </div>
          <StageProgress engine={props.stores.engine} onAnswerGate={answer} />
          <ArtifactPanel bridge={props.stores.bridge} taskId={engine.taskId} revision={engine.artifacts.length} />
          {!complete && status?.task?.status === "done" && engine.gates.length === 0 ? (
            <GatePanel ready gate={STAGE_LABELS[status.task.stage]} reason="阶段产出已完成，请检查后通过，或填写意见重新执行。"
              retries={0} onSubmit={(decision, feedback) => answer({ taskId: engine.taskId!, gate: status.task!.stage, decision, feedback })} />
          ) : null}
          {complete ? <p className="ok-text" role="status">四阶段已完成，可查看产物并导出合规声明。</p> : null}
          {complete || cancelled || status?.task?.status === "cancelled" ? <button type="button" className="btn" onClick={() => {
            props.stores.engine.setState({ ...initialEngineState }); setStatus(null); setComplete(false); setCancelled(false); draftId.current = null;
          }}>新建任务</button> : null}
          {engine.stage ? <p className="muted">当前阶段配额：{STAGE_POINTS[engine.stage]} 积分</p> : null}
        </>
      )}
    </div>
  );
}
