/**
 * 合规导出页（SP3-4 / US-007）：一键导出《AI 工具使用声明》，
 * 支持 Markdown/LaTeX/Word 三格式与「未使用 AI」模板；声明含工具清单、
 * 参与度与产物哈希（sha256）。异常态 = 导出失败可读原因。
 */

import { useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS, type ComplianceExportResult } from "../bridges/bridge.ts";
import { ErrorState } from "../components/states.tsx";
import type { AppStores } from "../state/app-stores.ts";

const FORMATS = [
  { value: "md", label: "Markdown" },
  { value: "latex", label: "LaTeX" },
  { value: "docx", label: "Word" },
] as const;

export function CompliancePage(props: { stores: AppStores }): ReactNode {
  const [format, setFormat] = useState<string>("md");
  const [unusedAi, setUnusedAi] = useState(false);
  const [humanNote, setHumanNote] = useState("");
  const [result, setResult] = useState<ComplianceExportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const doExport = async () => {
    // 新一轮导出先清旧结果：失败/校验拦截时不得残留上次声明预览（避免误以为已重新生成）
    setResult(null);
    setError(null);
    // 任务门禁：声明依据引擎留痕（audit_trail/artifact_index）按任务生成，无任务即无留痕可声明
    const taskId = props.stores.engine.getState().taskId;
    if (!taskId) {
      setError("暂无任务：请先在工作台运行任务，再生成声明（声明依据任务留痕逐条生成）。");
      return;
    }
    if (!humanNote.trim()) {
      setError("人工修改说明为必填项（2026 国赛规定），请填写后导出。");
      return;
    }
    setBusy(true);
    try {
      const value = await props.stores.bridge.invoke<ComplianceExportResult>(
        BRIDGE_CHANNELS.complianceExport,
        { format, unusedAi, humanNote, taskId },
      );
      setResult(value);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <h2>合规导出</h2>
      <div className="form-block">
        <label>
          导出格式
          <select value={format} onChange={(e) => setFormat(e.target.value)}>
            {FORMATS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check-row">
          <input type="checkbox" checked={unusedAi} onChange={(e) => setUnusedAi(e.target.checked)} />
          全程未使用 AI（生成「未使用 AI」版本模板）
        </label>
        <label>
          人工修改说明（必填 · 将写入声明）
          <textarea
            className="note-input"
            value={humanNote}
            onChange={(e) => setHumanNote(e.target.value)}
            placeholder="示例：数据预处理由本人手动完成；模型生成的图表经本人人工核验后使用。"
            rows={3}
          />
        </label>
        <div>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void doExport()}>
            生成声明
          </button>
        </div>
        <p className="muted">
          声明内容仅来自本机留痕库（audit_trail / artifact_index）与你的说明，条目与留痕一一对应；
          留痕损坏时提示重建，绝不伪造。
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