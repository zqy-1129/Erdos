/**
 * 引擎留痕读取（F-002 用量估算 + SP3-6 合规声明数据源）：只读访问引擎 audit.db。
 *
 * 数据边界（拟定基线）：
 * - 引擎（SP1-6 TrailRecorder）以 `ERDOS_ENGINE_HOME/audit.db` 落留痕（audit_trail +
 *   artifact_index，只追加）；本模块为主进程侧「留痕数据源端口（TrailSource）」实现，
 *   打开方式 readOnly，绝不写入引擎库；
 * - 声明导出（合规产物）任何读取失败一律抛出 → 导出层降级提示，绝不伪造；
 * - 用量估算（F-002）语义区分：库不存在 = 引擎未运行 → 无记录（空集，展示 0 是事实）；
 *   库存在但读取/解析失败 → 抛出（不把「未知」冒充为 0）。
 *
 * 并发：引擎以 rollback journal 写入，读连接设 busy_timeout（遇写锁等待而非立即失败）。
 * 本模块不依赖 electron（node:test 可加载）。
 */
import { existsSync } from "node:fs";
import { basename } from "node:path";
import { DatabaseSync } from "node:sqlite";
import {
  ComplianceDataError,
  DOCX_PREVIEW_SUFFIX,
  exportDeclaration,
  isDeclarationFormat,
  type ExportOptions,
} from "../declaration/export.ts";
import type { ArtifactEntry, ExportedDeclaration, TrailEvent, TrailSource } from "../declaration/types.ts";
import type { UsageEvent } from "../shared/usage.ts";

/** 引擎留痕库文件名（与 engine/__main__.py 的 TrailStore(home/"audit.db") 约定一致）。 */
export const ENGINE_TRAIL_DB = "audit.db";

/** 合法留痕事件类型（对齐引擎 trail.EventType 四类；漂移即抛错，不静默漏计）。 */
const EVENT_TYPES = new Set(["model_call", "tool_call", "artifact", "manual_edit"]);

/** 打开只读连接（busy_timeout 等待写锁；不存在/损坏时抛出）。 */
export function openReadOnly(dbPath: string): DatabaseSync {
  const db = new DatabaseSync(dbPath, { readOnly: true });
  db.exec("PRAGMA busy_timeout = 3000");
  return db;
}

/**
 * detail 文本 → 对象（引擎恒写对象；非对象按空对象容错，避免下游取字段崩溃）。
 * JSON 解析失败向上抛出，由调用方归一（声明→ComplianceDataError；用量→可读错误）。
 */
function parseDetail(raw: unknown): Record<string, unknown> {
  if (typeof raw !== "string") return {};
  const parsed = JSON.parse(raw) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  return parsed as Record<string, unknown>;
}

/**
 * 声明导出的留痕数据源（TrailSource 端口实现）。
 * 库缺失/损坏/字段漂移一律抛出（由 exportDeclaration 归一为可读降级提示）。
 */
export function createEngineTrailSource(dbPath: string): TrailSource {
  return {
    async events(taskId: string): Promise<TrailEvent[]> {
      const db = openReadOnly(dbPath);
      try {
        const rows = db
          .prepare(
            "SELECT id, task_id, stage, event_type, detail, ts FROM audit_trail " +
              "WHERE task_id = ? ORDER BY id",
          )
          .all(taskId) as Array<Record<string, unknown>>;
        try {
          return rows.map((row): TrailEvent => {
            const eventType = String(row["event_type"]);
            if (!EVENT_TYPES.has(eventType)) {
              // 版本漂移：不静默漏计（会低估声明参与度），且不误导为「库损坏可重建」（留痕只追加）
              throw new ComplianceDataError(
                `留痕事件类型未知（${eventType}）：数据可能来自不兼容版本，请升级客户端后再生成声明。`,
              );
            }
            return {
              id: Number(row["id"]),
              task_id: String(row["task_id"]),
              stage: String(row["stage"]),
              event_type: eventType as TrailEvent["event_type"],
              detail: parseDetail(row["detail"]),
              ts: String(row["ts"]),
            };
          });
        } catch (error) {
          if (error instanceof ComplianceDataError) throw error;
          throw new ComplianceDataError(
            "留痕记录解析失败（疑似损坏）：请检查数据目录；声明不会在留痕不完整时生成。",
            error,
          );
        }
      } finally {
        db.close();
      }
    },

    async artifacts(taskId: string): Promise<ArtifactEntry[]> {
      const db = openReadOnly(dbPath);
      try {
        const rows = db
          .prepare(
            "SELECT task_id, stage, kind, file_path, sha256, size_bytes FROM artifact_index " +
              "WHERE task_id = ? ORDER BY id",
          )
          .all(taskId) as Array<Record<string, unknown>>;
        return rows.map((row): ArtifactEntry => ({
          task_id: String(row["task_id"]),
          stage: String(row["stage"]),
          kind: String(row["kind"]),
          file_path: String(row["file_path"]),
          sha256: String(row["sha256"]),
          size_bytes: Number(row["size_bytes"]),
        }));
      } finally {
        db.close();
      }
    },
  };
}

/**
 * 读取全部 model_call 留痕（F-002 用量估算数据源）。
 * 库不存在（引擎未运行/未接线）→ 空集（0 是事实）；库存在但打开/读取/解析失败 → 抛出
 * （不把「未知」冒充为 0）。
 */
export function readUsageEvents(dbPath: string): UsageEvent[] {
  if (!existsSync(dbPath)) return [];
  try {
    const db = openReadOnly(dbPath);
    try {
      const rows = db
        .prepare("SELECT event_type, detail FROM audit_trail WHERE event_type = 'model_call' ORDER BY id")
        .all() as Array<Record<string, unknown>>;
      return rows.map((row) => ({
        event_type: String(row["event_type"]),
        detail: parseDetail(row["detail"]),
      }));
    } finally {
      db.close();
    }
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`本地留痕库读取失败：${reason}`, { cause: error });
  }
}

// ---------------------------------------------------------------------------
// 最近任务（合规声明任务来源：重启后引擎视图已清空，仍可对历史任务出声明）
// ---------------------------------------------------------------------------

/** 最近任务视图（渲染层 RecentTask 同形）。 */
export interface RecentTask {
  taskId: string;
  /** 最后一次留痕时间（引擎恒写 UTC ISO，同格式字符串序=时间序）。 */
  lastTs: string;
  /** 该任务留痕条数（选择时的完整性参考）。 */
  eventCount: number;
}

/**
 * 读取最近任务列表（按最后留痕时间倒序、并列按 task_id 升序确定；最多 limit 个）。
 * 库不存在（引擎未运行/未接线）→ 空集（无记录是事实）；库存在但读取失败 → 抛出
 * （不把「未知」冒充为「无任务」）。空 task_id 留痕行不可导出，SQL 层剔除。
 * 复杂度 O(n)（GROUP BY 扫描留痕行；n 为本地行数，单用户规模可接受，与 readUsageEvents 同口径）。
 */
export function readRecentTasks(dbPath: string, limit = 20): RecentTask[] {
  if (!existsSync(dbPath)) return [];
  try {
    const db = openReadOnly(dbPath);
    try {
      const rows = db
        .prepare(
          "SELECT task_id, MAX(ts) AS last_ts, COUNT(*) AS event_count FROM audit_trail " +
            "WHERE TRIM(task_id) <> '' GROUP BY task_id ORDER BY last_ts DESC, task_id ASC LIMIT ?",
        )
        .all(limit) as Array<Record<string, unknown>>;
      return rows.map((row): RecentTask => ({
        taskId: String(row["task_id"]),
        lastTs: String(row["last_ts"]),
        eventCount: Number(row["event_count"]),
      }));
    } finally {
      db.close();
    }
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`本地留痕库读取失败：${reason}`, { cause: error });
  }
}

/**
 * 从引擎留痕导出合规声明（SP3-6 收尾：真实数据源接线）。
 * taskId 必填（页面侧取自当前任务）；格式仅支持 md/latex/docx。
 */
export async function exportDeclarationFromEngineTrail(
  dbPath: string,
  options: ExportOptions,
): Promise<ExportedDeclaration> {
  if (!options.taskId.trim()) {
    throw new Error("暂无任务：请先在工作台运行任务后再生成声明（无任务无留痕可声明）");
  }
  if (!isDeclarationFormat(options.format)) {
    throw new Error(`不支持的导出格式：${String(options.format)}`);
  }
  return exportDeclaration(createEngineTrailSource(dbPath), options);
}

/** 桥视图（渲染层 ComplianceExportResult 同形）：声明导出后的可传输形态。 */
export interface ComplianceExportView {
  content: string;
  filename: string;
  artifactHashes: string[];
}

/** 声明导出入参（桥 payload 归一；format 为原始字符串以便白名单校验）。 */
export interface ComplianceExportOptions {
  taskId: string;
  format: string;
  humanNote: string;
  unusedAi: boolean;
}

/**
 * 声明导出 → 桥视图（格式白名单校验 + docx 预览策略 + 未使用 AI 不回传产物哈希）。
 * docx 为二进制：本页仅以同源 Markdown 预览并统一标注，文件名为 .docx；
 * 真实二进制落盘见 complianceSaveFromEngineTrail（本批已接线）。
 * 任何留痕读取失败经 ComplianceDataError 抛出（降级提示）。
 */
export async function complianceExportViewFromEngineTrail(
  dbPath: string,
  options: ComplianceExportOptions,
): Promise<ComplianceExportView> {
  const { taskId, humanNote, unusedAi } = options;
  if (!isDeclarationFormat(options.format)) {
    throw new Error(`不支持的导出格式：${options.format}`);
  }
  const previewFormat: "md" | "latex" | "docx" = options.format === "docx" ? "md" : options.format;
  const exported = await exportDeclarationFromEngineTrail(dbPath, {
    taskId,
    format: previewFormat,
    humanNote,
    unusedAi,
  });
  const content = String(exported.content);
  return {
    content: options.format === "docx" ? `${content}\n\n${DOCX_PREVIEW_SUFFIX}` : content,
    filename: options.format === "docx" ? exported.filename.replace(/\.md$/, ".docx") : exported.filename,
    artifactHashes: unusedAi ? [] : exported.data.artifactHashes.map((entry) => entry.sha256),
  };
}

// ---------------------------------------------------------------------------
// 声明文件真实落盘（SP3-6 收尾第二批：md/latex 文本 + docx 二进制）
// ---------------------------------------------------------------------------

/** 声明格式 → 保存对话框滤镜（原生保存对话框文件类型）。 */
const SAVE_FILTERS: Record<"md" | "latex" | "docx", { filterName: string; extensions: string[] }> = {
  md: { filterName: "Markdown 文件", extensions: ["md"] },
  latex: { filterName: "LaTeX 文件", extensions: ["tex"] },
  docx: { filterName: "Word 文档", extensions: ["docx"] },
};

/** 文件保存端口（主进程接线为原生保存对话框 + 写盘，见 main/save-export.ts；测试注入桩）。 */
export type DeclarationFileSaver = (request: {
  suggestedName: string;
  /** 文本（md/latex）或二进制（docx）。 */
  content: string | Uint8Array;
  filterName: string;
  extensions: string[];
}) => Promise<{ canceled: boolean; path: string | null }>;

/** 声明保存视图（渲染层 ComplianceSaveResult 同形）。 */
export interface ComplianceSaveView {
  /** 实际文件名（取消或未写盘时为建议文件名）。 */
  filename: string;
  /** 实际保存路径（用户取消为 null）。 */
  savedPath: string | null;
  canceled: boolean;
}

/**
 * 保存声明文件（SP3-6 收尾第二批）：三格式真实落盘——md/latex 为文本、
 * docx 为真实 zip 二进制（declaration/render.ts 的 renderDocx，非预览占位）；
 * 经注入保存器走原生保存对话框。用户取消 → canceled（不写盘、不视为失败）；
 * 写盘失败归一为「导出保存失败：…」可读错误；留痕缺失/损坏仍经 ComplianceDataError 降级。
 */
export async function complianceSaveFromEngineTrail(
  dbPath: string,
  options: ComplianceExportOptions,
  save: DeclarationFileSaver,
): Promise<ComplianceSaveView> {
  if (!isDeclarationFormat(options.format)) {
    throw new Error(`不支持的导出格式：${options.format}`);
  }
  // 真实格式导出（docx 内容为二进制）；taskId/人工说明门禁同预览路径（exportDeclarationFromEngineTrail）
  const exported = await exportDeclarationFromEngineTrail(dbPath, {
    taskId: options.taskId,
    format: options.format,
    humanNote: options.humanNote,
    unusedAi: options.unusedAi,
  });
  let result: { canceled: boolean; path: string | null };
  try {
    result = await save({
      suggestedName: exported.filename,
      content: exported.content,
      ...SAVE_FILTERS[options.format],
    });
  } catch (error) {
    // 落盘失败（磁盘/权限/对话框异常）归一为可读前缀（渲染层直接展示）
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`导出保存失败：${reason}`, { cause: error });
  }
  return {
    filename: result.path ? basename(result.path) : exported.filename,
    savedPath: result.path,
    canceled: result.canceled,
  };
}