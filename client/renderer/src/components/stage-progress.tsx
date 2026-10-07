/**
 * 四阶段进度渲染（SP3-4）：stage.progress 局部更新（进度条按字段更新）、
 * artifact.ready 追加产物、gate.failed 展示评审意见（US-004 rubric 可查）、
 * 长日志走虚拟列表。整块不整页刷新：只订阅 engine store。
 * C1 扩展：工具时间线（EventTimeline）+ 门禁审批（GatePanel）+ 流式输出（model.delta）。
 */

import { useState, type ReactNode } from "react";
import type { StageName } from "../../../shared/ipc.ts";
import type { Store } from "../storage/store.ts";
import { useStore } from "../storage/store.ts";
import type { EngineViewState } from "../state/engine-slice.ts";
import { EventTimeline } from "./event-timeline.tsx";
import { GatePanel } from "./gate-panel.tsx";
import { VirtualList } from "./virtual-list.tsx";

export const STAGES: StageName[] = ["analysis", "modeling", "solving", "writing"];
export const STAGE_LABELS: Record<StageName, string> = {
  analysis: "分析",
  modeling: "建模",
  solving: "求解",
  writing: "写作",
};

/** rubric 静态告知（US-004：门禁评分规则可见）。 */
const RUBRIC: Array<{ stage: StageName; rules: string[] }> = [
  { stage: "analysis", rules: ["题面要素完整提取", "数据特征与假设可解释", "研究问题聚焦且可验证"] },
  { stage: "modeling", rules: ["模型假设与数据一致", "目标函数含变量约束", "敏感性/合理性论证"] },
  { stage: "solving", rules: ["算法与模型匹配", "参数可复现", "结果量纲与数量级正确"] },
  { stage: "writing", rules: ["结构符合竞赛规范", "图表自明且编号齐全", "结论与数据互相印证"] },
];

/** 门禁审批提交参数（answer_gate 接线由 workspace 侧完成）。 */
export interface AnswerGateParams {
  taskId: string;
  gate: string;
  decision: "pass" | "reject";
  feedback: string;
}

export function StageProgress(props: {
  engine: Store<EngineViewState>;
  /** 提供时启用可交互审批（FE-APPROVE W13）；缺省退化为只读门禁列表。 */
  onAnswerGate?: (params: AnswerGateParams) => Promise<void>;
}): ReactNode {
  const state = useStore(props.engine);
  const [rubricOpen, setRubricOpen] = useState(false);
  const currentStageTools = state.stage ? (state.toolTimeline[state.stage] ?? []) : [];
  const pendingGate = state.gates.length > 0 ? state.gates[state.gates.length - 1] : null;

  return (
    <div className="stage-progress">
      <div className="stage-strip" role="progressbar" aria-label="四阶段进度">
        {STAGES.map((stage) => {
          const progress = state.stageProgress[stage] ?? 0;
          const active = state.stage === stage && state.running;
          return (
            <div key={stage} className={`stage-cell ${active ? "active" : ""}`}>
              <div className="stage-cell-head">
                <span>{STAGE_LABELS[stage]}</span>
                <span>{Math.round(progress * 100)}%</span>
              </div>
              <div className="stage-bar">
                <div className="stage-bar-fill" style={{ width: `${progress * 100}%` }} />
              </div>
            </div>
          );
        })}
      </div>

      <div className="stage-toolbar">
        <button type="button" className="btn" onClick={() => setRubricOpen((v) => !v)}>
          门禁评分规则（rubric）
        </button>
      </div>
      {rubricOpen ? (
        <div className="rubric">
          {RUBRIC.map((item) => (
            <div key={item.stage} className="rubric-row">
              <b>{STAGE_LABELS[item.stage]}</b>
              <ul>
                {item.rules.map((rule) => (
                  <li key={rule}>{rule}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : null}

      {state.gates.length > 0 ? (
        props.onAnswerGate && pendingGate ? (
          <GatePanel
            gate={STAGE_LABELS[pendingGate.gate as StageName] ?? pendingGate.gate}
            reason={pendingGate.reason}
            retries={state.gateRetries[pendingGate.gate] ?? 1}
            onSubmit={(decision, feedback) =>
              props.onAnswerGate!({
                taskId: state.taskId ?? "",
                gate: pendingGate.gate,
                decision,
                feedback,
              })
            }
          />
        ) : (
          <div className="gate-list">
            {state.gates.map((gate, i) => (
              <div key={`${gate.ts}-${i}`} className="gate-item">
                <b>门禁未通过（{STAGE_LABELS[gate.gate as StageName] ?? gate.gate}）</b>：{gate.reason}
              </div>
            ))}
          </div>
        )
      ) : null}

      <EventTimeline items={currentStageTools} />

      {state.modelOutput ? (
        <section className="model-output">
          <h3>模型输出（流式）</h3>
          <pre className="model-output-text">{state.modelOutput}</pre>
        </section>
      ) : null}

      <div className="stage-columns">
        <section className="artifact-panel">
          <h3>产物</h3>
          {state.artifacts.length === 0 ? (
            <div className="virtual-empty">尚无产物（artifact.ready 到达后展示）</div>
          ) : (
            <ul className="artifact-list">
              {state.artifacts.slice(-20).map((a, i) => (
                <li key={`${a.ts}-${i}`}>
                  <span className="tag">{a.kind}</span>
                  <span className="sha">sha256:{a.sha256.slice(0, 12)}…</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="log-panel">
          <h3>事件日志（{state.log.length}）</h3>
          <VirtualList
            items={state.log}
            rowHeight={26}
            height={220}
            renderRow={(line) => <span className="log-line">{line}</span>}
          />
        </section>
      </div>
    </div>
  );
}
