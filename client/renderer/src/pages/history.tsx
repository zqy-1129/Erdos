/** 本地历史：完整索引搜索、状态筛选、分页和既有许可入口续跑。 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS } from "../bridges/bridge.ts";
import type { StageName } from "../../../shared/ipc.ts";
import { HISTORY_LABELS, type HistoryPage as HistoryResult, type HistoryStatus, type HistoryTask } from "../../../shared/history.ts";
import { EmptyState, ErrorState } from "../components/states.tsx";
import { VirtualList } from "../components/virtual-list.tsx";
import { navigate } from "../router.tsx";
import type { AppStores } from "../state/app-stores.ts";

export function HistoryPage(props: { stores: AppStores }): ReactNode {
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<HistoryStatus>("all");
  const [criteria, setCriteria] = useState({ query: "", status: "all" as HistoryStatus });
  const [offset, setOffset] = useState(0);
  const [tick, setTick] = useState(0);
  const [items, setItems] = useState<HistoryTask[]>([]);
  const [page, setPage] = useState<HistoryResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [resumingId, setResumingId] = useState<string | null>(null);
  const resuming = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const timer = setTimeout(() => { setCriteria({ query: input, status }); setOffset(0); setResumeError(null); }, 250);
    return () => clearTimeout(timer);
  }, [input, status]);
  useEffect(() => {
    let disposed = false; setLoading(true); setError(null);
    if (offset === 0) { setItems([]); setPage(null); }
    void props.stores.bridge.invoke<HistoryResult>(BRIDGE_CHANNELS.historyQuery, { ...criteria, offset, limit: 50 }).then(result => {
      if (disposed) return;
      if (!result || !Array.isArray(result.items)) throw new Error("历史记录响应无效，请重试");
      setItems(previous => offset === 0 ? result.items : Array.from(new Map([...previous, ...result.items].map(item => [item.taskId, item])).values()));
      setPage(result); setLoading(false);
    }).catch(cause => { if (!disposed) { setError(cause instanceof Error ? cause.message : String(cause)); setLoading(false); } });
    return () => { disposed = true; };
  }, [props.stores, criteria.query, criteria.status, offset, tick]);
  const resume = async (task: HistoryTask) => {
    if (resuming.current) return;
    resuming.current = true; setResumingId(task.taskId); setResumeError(null);
    try {
      const result = await props.stores.bridge.invoke<{ taskId: string; stage: StageName }>(BRIDGE_CHANNELS.historyResume, { taskId: task.taskId });
      if (!mounted.current) return;
      props.stores.engine.setState({ taskId: result.taskId, stage: result.stage, running: true });
      navigate("/workspace");
    } catch (cause) { if (mounted.current) setResumeError(cause instanceof Error ? cause.message : String(cause)); }
    finally { resuming.current = false; if (mounted.current) setResumingId(null); }
  };
  return <div className="page">
    <h2>历史</h2>
    <div className="tabs">
      <label>搜索历史<input className="search" maxLength={128} value={input} disabled={resumingId !== null}
        placeholder="输入标题或任务编号" onChange={event => setInput(event.target.value)} /></label>
      <label>任务状态<select value={status} disabled={resumingId !== null} onChange={event => setStatus(event.target.value as HistoryStatus)}>
        {Object.entries(HISTORY_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
      </select></label>
      <button type="button" className="btn" disabled={loading || resumingId !== null}
        onClick={() => { setOffset(0); setTick(value => value + 1); }}>刷新历史</button>
    </div>
    {loading ? <p role="status">正在加载历史记录…</p> : null}
    {error ? <ErrorState title="历史记录加载失败" detail={error} onRetry={() => setTick(value => value + 1)} /> : null}
    {resumeError ? <p className="form-error" role="alert">{resumeError} · 可修正问题后重新继续</p> : null}
    {!loading && !error && items.length === 0 ? <EmptyState title={criteria.query || criteria.status !== "all" ? "没有匹配的任务" : "还没有任务"}
      hint={criteria.query || criteria.status !== "all" ? "换个关键词或任务状态试试。" : "完成首次运行后可在这里查看留痕与检查点。"} /> : null}
    {items.length > 0 ? <>
      <VirtualList key={JSON.stringify(criteria)} items={items} rowHeight={64} height={420} renderRow={task => <div className="history-row">
        <div className="history-main"><b>{task.title}</b><span className="muted">{task.updatedAt.slice(0, 16).replace("T", " ")} · {HISTORY_LABELS[task.status as HistoryStatus] ?? task.status}</span></div>
        {task.resumable ? <button type="button" className="btn" disabled={resumingId !== null}
          aria-label={`继续任务：${task.title}`} onClick={() => void resume(task)}>{resumingId === task.taskId ? "正在继续…" : "从检查点继续"}</button>
          : <span className="muted">{task.status === "done" ? "已完成" : "暂无检查点"}</span>}
      </div>} />
      <p role="status">已加载 {items.length} 条 · 匹配 {page?.total ?? items.length} 条</p>
      {page?.nextOffset !== null && page?.nextOffset !== undefined ? <button type="button" className="btn" disabled={loading || resumingId !== null}
        onClick={() => setOffset(page.nextOffset!)}>加载更多历史</button> : null}
    </> : null}
  </div>;
}
