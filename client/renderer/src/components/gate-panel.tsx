/**
 * 门禁审批面板（FE-APPROVE W13）：gate.failed 的可操作审批。
 *
 * reason 全文 + 修改意见 feedback（reject 必填，≤2048）+ pass/reject 双动作
 * + 重试计数 x/3（DEC-008：≤3 转人工）。提交经 onSubmit 回调（answer_gate 接线在
 * workspace 侧）；busy/error 三态，网络失败保留草稿不丢输入。
 */
import { useState, type ReactNode } from "react";

export interface GatePanelProps {
  gate: string;
  reason: string;
  /** 该门禁已失败次数（DEC-008 计数口径）。 */
  retries: number;
  maxRetries?: number;
  onSubmit: (decision: "pass" | "reject", feedback: string) => Promise<void>;
}

export function GatePanel(props: GatePanelProps): ReactNode {
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const maxRetries = props.maxRetries ?? 3;

  const submit = async (decision: "pass" | "reject"): Promise<void> => {
    if (decision === "reject" && !feedback.trim()) {
      setError("拒绝时必须填写修改意见（不可为空）");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await props.onSubmit(decision, feedback);
    } catch (e) {
      // 网络/冲突错误保留在表单，不丢已输入草稿（客户端方案 §7）
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gate-panel">
      <div className="gate-panel-head">
        <b>门禁未通过：{props.gate}</b>
        <span className="gate-retry">重试 {props.retries}/{maxRetries}</span>
      </div>
      <div className="gate-reason">{props.reason}</div>
      <label>
        修改意见（拒绝时必填）
        <textarea
          value={feedback}
          maxLength={2048}
          rows={3}
          placeholder="说明问题所在与修改方向，供下一轮重试参考"
          onChange={(e) => setFeedback(e.target.value)}
        />
      </label>
      {error ? <div className="form-error">{error}</div> : null}
      <div className="gate-actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy}
          onClick={() => void submit("pass")}
        >
          通过
        </button>
        <button
          type="button"
          className="btn btn-danger"
          disabled={busy}
          onClick={() => void submit("reject")}
        >
          拒绝并反馈
        </button>
      </div>
    </div>
  );
}
