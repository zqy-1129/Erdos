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
  const [result, setResult] = useState<ComplianceExportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const doExport = async () => {
    setBusy(true);
    setError(null);
    try {
      const value = await props.stores.bridge.invoke<ComplianceExportResult>(
        BRIDGE_CHANNELS.complianceExport,
        { format, unusedAi },
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
        <div>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void doExport()}>
            生成声明
          </button>
        </div>
      </div>

      {error ? <ErrorState title="导出失败" detail={error} onRetry={() => void doExport()} /> : null}

      {result ? (
        <div className="export-result">
          <h3>预览 · {result.filename}</h3>
          <pre className="export-preview">{result.content}</pre>
          <h3>产物哈希（可信声明依据）</h3>
          <ul>
            {result.artifactHashes.map((hash) => (
              <li key={hash} className="mono">sha256:{hash}</li>
            ))}
          </ul>
          <p className="muted">
            声明满足 2026 国赛新规：含工具清单、参与度与产物哈希；提交前请人工复核。
          </p>
        </div>
      ) : null}
    </div>
  );
}