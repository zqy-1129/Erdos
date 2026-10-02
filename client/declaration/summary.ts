/**
 * 留痕汇总（SP3-6）：audit_trail 事件 + artifact_index → DeclarationData。
 *
 * 可追溯红线：只统计留痕中真实存在的记录；留痕缺失时列表为空并在导出
 * 模板中显式呈现「无记录」，绝不编造条目。
 */

import type {
  ArtifactEntry,
  DeclarationData,
  StageParticipation,
  TrailEvent,
} from "./types.ts";

const STAGE_ORDER = ["analysis", "modeling", "solving", "writing"];

/** 汇总留痕为声明数据（纯函数，可单测）；taskId 取自调用方（留痕为空时不丢任务标识）。 */
export function summarizeTrail(
  events: TrailEvent[],
  artifacts: ArtifactEntry[],
  taskId: string,
): DeclarationData {

  const models = new Set<string>();
  const tools = new Set<string>();
  const stageMap = new Map<string, { modelCalls: number; toolCalls: number; manualEdits: number }>();

  for (const event of events) {
    if (event.event_type === "model_call") {
      const model = typeof event.detail["model"] === "string" ? (event.detail["model"] as string) : "";
      if (model) models.add(model);
    } else if (event.event_type === "tool_call") {
      const tool = typeof event.detail["tool"] === "string" ? (event.detail["tool"] as string) : "";
      if (tool) tools.add(tool);
    }
    const count = stageMap.get(event.stage) ?? { modelCalls: 0, toolCalls: 0, manualEdits: 0 };
    if (event.event_type === "model_call") count.modelCalls += 1;
    else if (event.event_type === "tool_call") count.toolCalls += 1;
    else if (event.event_type === "manual_edit") count.manualEdits += 1;
    stageMap.set(event.stage, count);
  }

  const participation: StageParticipation[] = [...stageMap.entries()]
    .sort(([a], [b]) => STAGE_ORDER.indexOf(a) - STAGE_ORDER.indexOf(b))
    .map(([stage, count]) => ({ stage, ...count }));

  return {
    taskId,
    toolList: [...models, ...tools],
    stageParticipation: participation,
    artifactHashes: artifacts.map((artifact) => ({
      kind: artifact.kind,
      sha256: artifact.sha256,
    })),
    trailCount: events.length,
  };
}