/**
 * 声明导出编排（SP3-6 / DF-010）：
 * 留痕读取 → 汇总 → 人工说明必填校验 → 三格式渲染。
 *
 * 降级红线：留痕库读取失败（损坏/缺失）抛出 ComplianceDataError 并明确
 * 提示重建，绝不返回伪造声明；人工修改说明为必填项（2026 国赛规定）。
 */

import { renderDocx, renderLatex, renderMarkdown } from "./render.ts";
import { summarizeTrail } from "./summary.ts";
import type {
  DeclarationData,
  DeclarationFormat,
  ExportedDeclaration,
  TrailSource,
} from "./types.ts";

/** 留痕数据不可用（损坏/缺失）：提示重建，禁止伪造。 */
export class ComplianceDataError extends Error {
  readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "ComplianceDataError";
    this.cause = cause;
  }
}

const MIME: Record<DeclarationFormat, string> = {
  md: "text/markdown",
  latex: "application/x-latex",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

const EXT: Record<DeclarationFormat, string> = {
  md: "md",
  latex: "tex",
  docx: "docx",
};

/** 运行时格式白名单校验（桥 payload 为原始字符串，须校验后才可调用类型化导出）。 */
const DECLARATION_FORMATS = ["md", "latex", "docx"] as const;

/** 声明格式白名单校验（主进程桥与演示桥共用，避免多处硬编码漂移）。 */
export function isDeclarationFormat(value: string): value is DeclarationFormat {
  return (DECLARATION_FORMATS as readonly string[]).includes(value);
}

/** docx 为二进制：预览页以同源 Markdown 展示时的统一标注（主进程桥与演示桥共用文案）。 */
export const DOCX_PREVIEW_SUFFIX =
  "（Word 为二进制格式：本页仅以同源 Markdown 预览；保存文件将生成真正的 .docx 文档。）";

export interface ExportOptions {
  taskId: string;
  format: DeclarationFormat;
  /** 用户人工修改说明（必填，DF-010）。 */
  humanNote: string;
  /** 未使用 AI 版本模板。 */
  unusedAi: boolean;
  /** 生成时间（注入便于测试快照）。 */
  generatedAt?: string;
}

function checkNote(note: string): void {
  if (!note.trim()) {
    throw new ComplianceDataError("人工修改说明为必填项（2026 国赛规定），请在导出前填写。");
  }
}

/** 读取留痕并导出声明；任何留痕读取失败都转为可读的降级提示。 */
export async function exportDeclaration(
  source: TrailSource,
  options: ExportOptions,
): Promise<ExportedDeclaration> {
  checkNote(options.humanNote);

  let events;
  let artifacts;
  try {
    events = await source.events(options.taskId);
    artifacts = await source.artifacts(options.taskId);
  } catch (error) {
    // 已是可读降级（如版本不兼容/记录解析失败）：保留具体归因，避免误导为「库损坏可重建」
    if (error instanceof ComplianceDataError) throw error;
    throw new ComplianceDataError(
      "本地留痕库不可用（可能已损坏）：请检查数据目录并在「设置」中重建留痕库后重试。声明不会在留痕缺失时伪造条目。",
      error,
    );
  }

  const data: DeclarationData = summarizeTrail(events, artifacts, options.taskId);
  const generatedAt = options.generatedAt ?? new Date().toISOString();

  let content: string | Uint8Array;
  if (options.format === "md") {
    content = renderMarkdown({ data, format: "md", humanNote: options.humanNote, unusedAi: options.unusedAi, generatedAt });
  } else if (options.format === "latex") {
    content = renderLatex({ data, format: "latex", humanNote: options.humanNote, unusedAi: options.unusedAi, generatedAt });
  } else {
    content = await renderDocx({ data, format: "docx", humanNote: options.humanNote, unusedAi: options.unusedAi, generatedAt });
  }

  return {
    filename: `AI工具使用声明_${options.taskId}.${EXT[options.format]}`,
    mime: MIME[options.format],
    content,
    data,
  };
}