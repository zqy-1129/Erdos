/**
 * 合规声明领域类型与留痕数据源端口（SP3-6）。
 *
 * 数据来源红线（执行计划）：
 * - 声明字段只来自留痕库且只覆盖可追溯内容；
 * - 留痕缺失/损坏时提示重建，禁止编造条目；
 * - 留痕仅存本地，永不上传（合规数据本地性）。
 */

/** 留痕事件（对齐引擎 trail EventType 四类 + 客户端 migrations audit_trail 表）。 */
export interface TrailEvent {
  id: number;
  task_id: string;
  stage: string;
  event_type: "model_call" | "tool_call" | "artifact" | "manual_edit";
  detail: Record<string, unknown>;
  ts: string;
}

/** 产物索引条目（对齐 artifact_index 表）。 */
export interface ArtifactEntry {
  task_id: string;
  stage: string;
  kind: string;
  file_path: string;
  sha256: string;
  size_bytes: number;
}

/**
 * 留痕数据源端口：主进程侧 SQLite 实现（audit_trail + artifact_index）；
 * 任何读取失败都必须抛出——导出层据此降级提示，绝不返回伪造数据。
 */
export interface TrailSource {
  events(taskId: string): Promise<TrailEvent[]>;
  artifacts(taskId: string): Promise<ArtifactEntry[]>;
}

/** 声明导出格式。 */
export type DeclarationFormat = "md" | "latex" | "docx";

/** 声明数据（留痕汇总产物：可追溯、无虚构字段）。 */
export interface DeclarationData {
  taskId: string;
  /** 工具清单：模型（去重）+ 工具（去重）。 */
  toolList: string[];
  /** 各阶段 AI 参与度（事件计数）。 */
  stageParticipation: StageParticipation[];
  /** 产物哈希支撑材料。 */
  artifactHashes: Array<{ kind: string; sha256: string }>;
  /** 留痕事件总数。 */
  trailCount: number;
}

export interface StageParticipation {
  stage: string;
  modelCalls: number;
  toolCalls: number;
  manualEdits: number;
}

/** 导出产物。 */
export interface ExportedDeclaration {
  filename: string;
  mime: string;
  /** md/latex 为字符串；docx 为 Uint8Array。 */
  content: string | Uint8Array;
  data: DeclarationData;
}