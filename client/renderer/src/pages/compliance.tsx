/**
 * 合规导出页（SP3-4 / US-007）：一键导出《AI 工具使用声明》，
 * 支持 Markdown/LaTeX/Word 三格式与「未使用 AI」模板；声明含工具清单、
 * 参与度与产物哈希（sha256）。异常态 = 导出失败可读原因。
 * 生成声明 = 页面预览；保存文件 = 经主进程原生保存对话框真实落盘
 * （md/latex 文本、docx 真实二进制，见 compliance:save）。
 * 任务来源 = 本次会话任务或本地留痕「最近任务」（重启后仍可对历史任务出声明）。
 */

import { useState, type ReactNode } from "react";
import {
  BRIDGE_CHANNELS,
  type ComplianceExportResult,
  type ComplianceSaveResult,
  type RecentTask,
} from "../bridges/bridge.ts";
import { ErrorState } from "../components/states.tsx";
import { useRemoteData } from "../components/use-remote.ts";
import { useStore } from "../storage/store.ts";
import type { AppStores } from "../state/app-stores.ts";

const FORMATS = [
  { value: "md", label: "Markdown" },
  { value: "latex", label: "LaTeX" },
  { value: "docx", label: "Word" },
] as const;

/** 留痕时间展示（引擎恒写 UTC ISO；截断到分钟）。 */
function formatTs(ts: string): string {
  return ts ? ts.slice(0, 16).replace("T", " ") : "时间未知";
}

export function CompliancePage(props: { stores: AppStores }): ReactNode {
  const [format, setFormat] = useState<string>("md");
  const [unusedAi, setUnusedAi] = useState(false);
  const [humanNote, setHumanNote] = useState("");
  const [result, setResult] = useState<ComplianceExportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [pickedTaskId, setPickedTaskId] = useState<string | null>(null);

  const engine = useStore(props.stores.engine);
  const recent = useRemoteData<RecentTask[]>(() =>
    props.stores.bridge.invoke<RecentTask[]>(BRIDGE_CHANNELS.trailRecentTasks),
  );
  const sessionTaskId = engine.taskId;
  /** 最近留痕任务（会话任务已单列，去掉重复项）。 */
  const recentTasks = (recent.data ?? []).filter((task) => task.taskId !== sessionTaskId);
  /**
   * 有效任务号：显式选择 > 本次会话；显式选择须仍在选项内
   * （防列表变化后放行一个界面不可见/不可选的任务），均无则 null（门禁拦截，不猜测任务）。
   */
  const taskId =
    pickedTaskId !== null && (pickedTaskId === sessionTaskId || recentTasks.some((task) => task.taskId === pickedTaskId))
      ? pickedTaskId
      : sessionTaskId;
  /** 占位项文案（受加载/失败态约束，避免误报「未发现任务」）。 */
  const taskPlaceholder = recent.loading
    ? "正在读取本机留痕…"
    : recent.error
      ? "最近任务读取失败（见下方提示）"
      : recentTasks.length > 0
        ? "请选择任务（本机最近留痕）"
        : "本地留痕中未发现任务";

  /**
   * 导出/保存共用门禁：声明依据引擎留痕（audit_trail/artifact_index）按任务生成，
   * 无任务即无留痕可声明（可选本次会话任务或本地留痕历史任务）；
   * 人工修改说明为 2026 国赛新规必填项。
   */
  const checkGate = (): { taskId: string } | { reason: string } => {
    if (!taskId) {
      return { reason: "暂无任务：请先在工作台运行任务，或在「任务来源」中选择最近留痕任务。" };
    }
    if (!humanNote.trim()) {
      return { reason: "人工修改说明为必填项（2026 国赛规定），请填写后导出。" };
    }
    return { taskId };
  };

  /**
   * 输入变更：预览与保存提示均对应旧输入（任务/格式/说明），全部清除，
   * 避免「预览的是 A、保存的是 B」与过期「已保存至」误导（需重新生成再复核）。
   */
  const invalidateOutputs = (): void => {
    setResult(null);
    setSavedMsg(null);
    setSaveError(null);
  };

  /** 生成声明预览：新一轮先清旧结果（失败/拦截不得残留上次预览，避免误以为已重新生成）。 */
  const doExport = async () => {
    setResult(null);
    setError(null);
    const gate = checkGate();
    if ("reason" in gate) {
      setError(gate.reason);
      return;
    }
    setBusy(true);
    try {
      const value = await props.stores.bridge.invoke<ComplianceExportResult>(
        BRIDGE_CHANNELS.complianceExport,
        { format, unusedAi, humanNote, taskId: gate.taskId },
      );
      setResult(value);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  /**
   * 保存声明文件（compliance:save）：主进程生成真实内容（Word 为 .docx 二进制）并经
   * 原生保存对话框落盘。仅清本动作旧提示（预览结果不受保存动作影响）；
   * 用户取消 → 静默（不提示失败）；失败 → 可读原因。
   */
  const doSave = async () => {
    setSavedMsg(null);
    setSaveError(null);
    const gate = checkGate();
    if ("reason" in gate) {
      setSaveError(gate.reason);
      return;
    }
    setSaving(true);
    try {
      const value = await props.stores.bridge.invoke<ComplianceSaveResult>(
        BRIDGE_CHANNELS.complianceSave,
        { format, unusedAi, humanNote, taskId: gate.taskId },
      );
      if (value.canceled) return; // 用户在保存对话框中取消：静默（不提示失败）
      setSavedMsg(value.savedPath ? `已保存至 ${value.savedPath}` : `已生成 ${value.filename}`);
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <h2>合规导出</h2>
      <div className="form-block">
        <label>
          任务来源（本地留痕 · 声明按所选任务生成）
          <select
            aria-label="任务来源"
            value={taskId ?? ""}
            onChange={(e) => {
              setPickedTaskId(e.target.value === "" ? null : e.target.value);
              invalidateOutputs();
            }}
          >
            {sessionTaskId ? null : <option value="">{taskPlaceholder}</option>}
            {sessionTaskId ? <option value={sessionTaskId}>当前任务：{sessionTaskId}（本次会话）</option> : null}
            {recentTasks.map((task) => (
              <option key={task.taskId} value={task.taskId}>
                {task.taskId}（最近活动 {formatTs(task.lastTs)} · {task.eventCount} 条留痕）
              </option>
            ))}
          </select>
        </label>
        {recent.error ? <p className="form-error">最近任务读取失败：{recent.error}</p> : null}
        {!sessionTaskId && !recent.loading && !recent.error && recentTasks.length === 0 ? (
          <p className="muted">本机留痕中还没有任务记录：先在工作台运行任务，留痕生成后可在此选择任务出声明。</p>
        ) : null}
        <label>
          导出格式
          <select
            value={format}
            onChange={(e) => {
              setFormat(e.target.value);
              invalidateOutputs();
            }}
          >
            {FORMATS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={unusedAi}
            onChange={(e) => {
              setUnusedAi(e.target.checked);
              invalidateOutputs();
            }}
          />
          全程未使用 AI（生成「未使用 AI」版本模板）
        </label>
        <label>
          人工修改说明（必填 · 将写入声明）
          <textarea
            className="note-input"
            value={humanNote}
            onChange={(e) => {
              setHumanNote(e.target.value);
              invalidateOutputs();
            }}
            placeholder="示例：数据预处理由本人手动完成；模型生成的图表经本人人工核验后使用。"
            rows={3}
          />
        </label>
        <div className="compliance-actions">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || saving}
            onClick={() => void doExport()}
          >
            生成声明
          </button>
          <button type="button" className="btn" disabled={busy || saving} onClick={() => void doSave()}>
            保存文件
          </button>
          {savedMsg ? <span className="ok-text">{savedMsg}</span> : null}
          {saveError ? <span className="form-error">{saveError}</span> : null}
        </div>
        <p className="muted">
          声明内容仅来自本机留痕库（audit_trail / artifact_index）与你的说明，条目与留痕一一对应；
          留痕损坏时提示重建，绝不伪造。保存文件按所选格式落盘：Markdown / LaTeX 为文本，
          Word 为真实 .docx 二进制。
        </p>
      </div>

      {error ? <ErrorState title="导出失败" detail={error} onRetry={() => void doExport()} /> : null}

      {result ? (
        <div className="export-result">
          <h3>预览 · {result.filename}</h3>
          <pre className="export-preview">{result.content}</pre>
          {result.artifactHashes.length > 0 ? (
            <>
              <h3>产物哈希（可信声明依据）</h3>
              <ul>
                {result.artifactHashes.map((hash) => (
                  <li key={hash} className="mono">sha256:{hash}</li>
                ))}
              </ul>
            </>
          ) : null}
          <p className="muted">
            {unusedAi
              ? "未使用 AI 版本：声明不含工具清单、参与度与产物哈希；提交前请人工复核。"
              : "声明满足 2026 国赛新规：含工具清单、参与度与产物哈希；提交前请人工复核。"}
          </p>
        </div>
      ) : null}
    </div>
  );
}