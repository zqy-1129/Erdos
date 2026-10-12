/**
 * 门禁审批面板（FE-APPROVE W13）：gate.failed 的可操作审批。
 *
 * reason 全文 + 修改意见 feedback（reject 必填，≤2048）+ pass/reject 双动作
 * + 重试计数 x/3（DEC-008：≤3 转人工）。提交经 onSubmit 回调（answer_gate 接线在
 * workspace 侧）；busy/error 三态，网络失败保留草稿不丢输入。
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export interface GatePanelProps {
  /** 每个任务/门禁尝试唯一；切换后草稿、错误和旧提交不跨操作复用。 */
  scopeKey?: string;
  gate: string;
  ready?: boolean;
  reason: string;
  /** 该门禁已失败次数（DEC-008 计数口径）。 */
  retries: number;
  maxRetries?: number;
  onSubmit: (decision: "pass" | "reject", feedback: string) => Promise<void>;
}

export function GatePanel(props: GatePanelProps): ReactNode {
  return <GatePanelForm key={props.scopeKey ?? `${props.gate}:${props.retries}:${props.ready}`} {...props} />;
}

function GatePanelForm(props: GatePanelProps): ReactNode {
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  const mounted = useRef(true);
  const feedbackInput = useRef<HTMLTextAreaElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const id = useId();
  useEffect(() => { mounted.current = true; heading.current?.focus(); return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (error && !busy) feedbackInput.current?.focus(); }, [error, busy]);
  const maxRetries = props.maxRetries ?? 3;

  const submit = async (decision: "pass" | "reject"): Promise<void> => {
    if (submitting.current) return;
    if (decision === "reject" && !feedback.trim()) {
      setError("拒绝时必须填写修改意见（不可为空）");
      return;
    }
    submitting.current = true; setBusy(true);
    setError(null);
    try {
      await props.onSubmit(decision, feedback);
    } catch (e) {
      // 网络/冲突错误保留在表单，不丢已输入草稿（客户端方案 §7）
      if (mounted.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      submitting.current = false; if (mounted.current) setBusy(false);
    }
  };

  return (
    <section className="gate-panel" aria-labelledby={id + "-heading"} aria-busy={busy}>
      <div className="gate-panel-head">
        <h3 id={id + "-heading"} ref={heading} tabIndex={-1}>{props.ready ? "阶段待审批" : "门禁未通过"}：{props.gate}</h3>
        <span className="gate-retry">重试 {props.retries}/{maxRetries}</span>
      </div>
      <div className="gate-reason" id={id + "-reason"}>{props.reason}</div>
      <label>
        修改意见（拒绝时必填）
        <textarea
          ref={feedbackInput}
          disabled={busy}
          aria-describedby={id + "-reason" + (error ? " " + id + "-error" : "")}
          value={feedback}
          maxLength={2048}
          rows={3}
          placeholder="说明问题所在与修改方向"
          onChange={(e) => setFeedback(e.target.value)}
        />
      </label>
      {error ? <div className="form-error" id={id + "-error"} role="alert">{error}</div> : null}
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
    </section>
  );
}
