/**
 * 本地历史数据源（US-003 检查点继续，客户端侧）：引擎 home 下本地库只读组合读取——
 * - audit.db（留痕）：任务最近活动（复用 readRecentTasks）；
 * - checkpoints.db（检查点库）：题面标题（task_inputs）与阶段门禁状态（恢复定位）。
 *
 * 口径（与 engine-trail.ts 一致，fail-safe）：
 * - 库缺失 = 无记录（空集 / 不可恢复）；
 * - 库存在但读取失败 = 抛出（不把「未知」冒充为「无任务」）。
 *
 * 跨端常量：检查点「门禁通过」状态 = 引擎 StageStatus.DONE（"done"；见 engine/orchestrator/graph.py
 * CHECKPOINT_GATE_PASSED；"stage_done"=执行完未过门禁）——按引擎「未经门禁不推进」语义视为未完成。
 * 恢复定位前提：检查点按阶段连续落盘（引擎 restore 同前提）；存在空洞时引擎以可读顺序约束错误拒发，
 * 桥层不吞错（用户可见「阶段顺序约束」提示，不会静默双跑）。
 * 版本偏斜：旧 home 无 task_inputs 表（该表随题面持久化新增）→ 视为无题面记录（标题回退 taskId），
 * 阶段读取不受影响；库损坏仍抛出（见 readCheckpointData）。
 *
 * 边界：零引擎改动、零契约变更——续跑调用复用既有 RPC（start_stage），
 * 检查点读取与题面水合由引擎侧承接（本模块只读，不写任何引擎文件）。
 */

import { existsSync } from "node:fs";

import { openReadOnly, readRecentTasks } from "./engine-trail.ts";
import type { StageName } from "../shared/ipc.ts";

/** 四阶段顺序（恢复定位依据；与引擎 STAGES 同序）。 */
const STAGES: readonly StageName[] = ["analysis", "modeling", "solving", "writing"];

/** 门禁通过状态（引擎 StageStatus.DONE.value）。 */
const CHECKPOINT_GATE_PASSED = "done";

/** 历史任务视图（渲染层 HistoryTask 同形；主进程不依赖渲染层类型）。 */
export interface LocalHistoryTask {
  taskId: string;
  title: string;
  /** 任务当前阶段名（未产出检查点任务为 "created"；四阶段均过门禁为 "done"）。 */
  status: string;
  /** 最后一次留痕时间（UTC ISO；与引擎恒写格式一致）。 */
  updatedAt: string;
  /** 是否存在可续检查点（至少一个检查点且未全部过门禁）。 */
  resumable: boolean;
}

/** 检查点库全量读取结果（单用户规模小表：一次读取后内存定位）。 */
interface CheckpointData {
  /** task_id → 题面标题。 */
  titles: Map<string, string>;
  /** task_id →（stage → status）。 */
  stages: Map<string, Map<string, string>>;
}

/**
 * 读取检查点库（题面标题 + 阶段状态）。库缺失→空数据；读取失败→抛出（可读错误）。
 * 复杂度 O(n)（全表扫描；n = 任务数 × ≤4 阶段，单用户规模可接受，与 readRecentTasks 同口径）。
 */
function readCheckpointData(dbPath: string): CheckpointData {
  if (!existsSync(dbPath)) return { titles: new Map(), stages: new Map() };
  try {
    const db = openReadOnly(dbPath);
    try {
      const titles = new Map<string, string>();
      try {
        const titleRows = db
          .prepare("SELECT task_id, title FROM task_inputs")
          .all() as Array<Record<string, unknown>>;
        for (const row of titleRows) {
          titles.set(String(row["task_id"]), String(row["title"]));
        }
      } catch {
        // 版本偏斜容错：旧引擎 home 无 task_inputs 表 → 无题面记录（标题回退 taskId）。
        // 库损坏/锁超时会在下方 checkpoints 查询处照常抛出，不掩盖真实故障。
      }
      const stages = new Map<string, Map<string, string>>();
      const stageRows = db
        .prepare("SELECT task_id, stage, status FROM checkpoints")
        .all() as Array<Record<string, unknown>>;
      for (const row of stageRows) {
        const taskId = String(row["task_id"]);
        const perTask = stages.get(taskId) ?? new Map<string, string>();
        perTask.set(String(row["stage"]), String(row["status"]));
        stages.set(taskId, perTask);
      }
      return { titles, stages };
    } finally {
      db.close();
    }
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`本地检查点库读取失败：${reason}`, { cause: error });
  }
}

/** 恢复定位（纯函数）：第一个「非门禁通过」阶段（含未开始与执行完未过门禁；全部通过→null）。 */
export function deriveResumeStage(stageStates: ReadonlyMap<string, string>): StageName | null {
  for (const stage of STAGES) {
    if (stageStates.get(stage) !== CHECKPOINT_GATE_PASSED) return stage;
  }
  return null;
}

/** 任务视图派生（纯函数）：无检查点=created 不可续；全部过门禁=done 不可续；否则=下一阶段可续。 */
function deriveTaskView(stageStates: ReadonlyMap<string, string> | undefined): {
  status: string;
  resumable: boolean;
} {
  if (stageStates === undefined || stageStates.size === 0) return { status: "created", resumable: false };
  const next = deriveResumeStage(stageStates);
  return next === null ? { status: "done", resumable: false } : { status: next, resumable: true };
}

/**
 * 读取本地历史任务（近期留痕任务 × 检查点元数据；按最后留痕时间倒序，最多 limit 个）。
 * 留痕库口径（缺失=空、失败=抛出）复用 readRecentTasks；检查点库缺失时标题回退 taskId、
 * 任务视为不可续（无可恢复检查点是事实，不虚构）。
 */
export function readLocalHistory(trailDbPath: string, checkpointsDbPath: string, limit = 50): LocalHistoryTask[] {
  const recent = readRecentTasks(trailDbPath, limit);
  if (recent.length === 0) return [];
  const data = readCheckpointData(checkpointsDbPath);
  return recent.map((task) => {
    const view = deriveTaskView(data.stages.get(task.taskId));
    return {
      taskId: task.taskId,
      title: data.titles.get(task.taskId) ?? task.taskId, // 缺题面（历史遗留）回退任务号，不虚构标题
      status: view.status,
      updatedAt: task.lastTs,
      resumable: view.resumable,
    };
  });
}

/**
 * 解析续跑目标阶段：无可恢复检查点（库缺失 / 任务无检查点 / 四阶段已全过）→ null。
 * 调用方（history:resume）据 null 给出可读提示，不猜测阶段。
 */
export function resolveResumeStage(checkpointsDbPath: string, taskId: string): StageName | null {
  const data = readCheckpointData(checkpointsDbPath);
  const stageStates = data.stages.get(taskId);
  if (stageStates === undefined || stageStates.size === 0) return null;
  return deriveResumeStage(stageStates);
}

/** 只读门禁提交状态，用于崩溃后的扣费确认；查询失败必须阻止结算。 */
export function checkpointStageStatus(dbPath: string, taskId: string, stage: StageName): string | null {
  return readCheckpointData(dbPath).stages.get(taskId)?.get(stage) ?? null;
}
