/**
 * 工作台（SP3-4 / US-001/US-003/US-004）：
 * - 空态：示例题卡片（题面+数据摘要）与「一键开始四阶段」；
 * - 运行态：四阶段进度 + 产物 + 门禁评审（StageProgress 局部更新）；
 * - 异常态：启动失败可读原因；断网态：全局横幅（layout）。
 * C1 扩展（FE-APPROVE）：门禁审批 answer_gate 接线 + 冲突自动刷新。
 */

import { useState, type ReactNode } from "react";
import { useStore } from "../storage/store.ts";
import { StageProgress, type AnswerGateParams } from "../components/stage-progress.tsx";
import { EmptyState, ErrorState } from "../components/states.tsx";
import { applyStatusSnapshot, isGateConflict } from "../state/engine-slice.ts";
import type { AppStores } from "../state/app-stores.ts";
import type { AnswerGateResult, GetStatusResult } from "../../../shared/ipc.ts";

export function WorkspacePage(props: { stores: AppStores }): ReactNode {
  const engine = useStore(props.stores.engine);
  const [startError, setStartError] = useState<string | null>(null);

  const start = async () => {
    setStartError(null);
    try {
      await props.stores.bridge.invoke("engine:start_stage", {
        task_id: "demo-task",
        stage: "analysis",
      });
    } catch (error) {
      setStartError(error instanceof Error ? error.message : String(error));
    }
  };

  /** 用 get_status 快照收敛引擎视图状态（冲突/轮询后刷新，不依赖事件）。 */
  const refreshEngineStatus = async (): Promise<void> => {
    const status = await props.stores.bridge.invoke<GetStatusResult>("engine:get_status", {});
    props.stores.engine.setState(applyStatusSnapshot(props.stores.engine.getState(), status));
  };

  /** 门禁审批提交（FE-APPROVE W13）：answer_gate → 冲突自动刷新 → reject 重试续跑。 */
  const onAnswerGate = async (params: AnswerGateParams): Promise<void> => {
    try {
      const result = await props.stores.bridge.invoke<AnswerGateResult>("engine:answer_gate", {
        task_id: params.taskId,
        gate: params.gate,
        decision: params.decision,
        feedback: params.feedback,
      });
      if (result.action === "retry_stage") {
        await props.stores.bridge.invoke("engine:start_stage", {
          task_id: params.taskId,
          stage: params.gate,
        });
      }
      await refreshEngineStatus();
    } catch (error) {
      if (isGateConflict(error)) {
        // 冲突（重复/过期提交）：自动 get_status 刷新一次收敛
        await refreshEngineStatus();
      }
      throw error;
    }
  };

  if (startError) {
    return <ErrorState title="示例题启动失败" detail={startError} onRetry={() => void start()} />;
  }

  if (engine.taskId === null) {
    return (
      <div className="page">
        <h2>工作台</h2>
        <EmptyState
          title="从示例题开始（注册赠 100 积分已到账）"
          hint="内置一道示例题（含题面+数据），一键开始四阶段：分析 → 建模 → 求解 → 写作。门禁自动检查各阶段产出质量，不合格自动重试并给出具体问题。"
          action={
            <button type="button" className="btn btn-primary" onClick={() => void start()}>
              一键开始四阶段
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="page">
      <h2>工作台{engine.taskId === "demo-task" ? " · 示例题：嫦娥三号软着陆" : ""}</h2>
      <StageProgress engine={props.stores.engine} onAnswerGate={onAnswerGate} />
    </div>
  );
}
