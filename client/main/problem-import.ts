import { readFile, stat } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { basename, extname } from "node:path";
import { extractInProcess, type ExtractionContext } from "./problem-extraction-host.ts";
import { parseImportId, type ImportProgress, type ImportedProblem } from "../shared/problem-import.ts";
export { extractDocxText, validateDocxBudget, validateImageBudget } from "./problem-extraction.ts";
export type { ImportedProblem } from "../shared/problem-import.ts";

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const MAX_TEXT_CHARS = 60000;
export interface ProblemImportOptions {
  select: () => Promise<string | null>;
  ocrCachePath: string;
  timeoutMs?: number;
  extract?: (extension: string, bytes: Buffer, context: ExtractionContext) => Promise<string>;
}
class ImportCancelledError extends Error { constructor() { super("题面导入已取消"); } }
class ImportTimeoutError extends Error { constructor() { super("题面解析超时，请拆分文件或重试"); } }
interface ImportOperation { id: string; controller: AbortController; status: ImportProgress }

/** 等待可以中止；迟到的解析结果被丢弃，不改变之后的导入状态。 */
function interruptible<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
  signal.throwIfAborted();
  return new Promise<T>((resolve, reject) => {
    const abort = () => { signal.removeEventListener("abort", abort); reject(signal.reason); };
    signal.addEventListener("abort", abort, { once: true });
    task.then(value => { signal.removeEventListener("abort", abort); resolve(value); },
      error => { signal.removeEventListener("abort", abort); reject(error); });
  });
}

/** 单次导入协调器：文件选择、限额、进度、取消和处理期限；解析器及缓存实现可替换。 */
export class ProblemImporter {
  private readonly options: ProblemImportOptions;
  private readonly timeoutMs: number;
  private active: ImportOperation | null = null;
  private last: ImportProgress | null = null;
  private closed = false;
  constructor(options: ProblemImportOptions) {
    this.options = options;
    this.timeoutMs = options.timeoutMs ?? 120000;
    if (!Number.isSafeInteger(this.timeoutMs) || this.timeoutMs < 1 || this.timeoutMs > 600000) throw new Error("导入处理时限无效");
  }
  status(importId: unknown): ImportProgress | null {
    const id = parseImportId(importId);
    const status = this.active?.status.importId === id ? this.active.status : this.last?.importId === id ? this.last : null;
    return status ? { ...status } : null;
  }
  cancel(importId: unknown): { cancelled: boolean } {
    const id = parseImportId(importId);
    if (!this.active || this.active.id !== id || this.active.controller.signal.aborted) return { cancelled: false };
    this.active.controller.abort(new ImportCancelledError());
    return { cancelled: true };
  }
  dispose(): void { this.closed = true; this.active?.controller.abort(new ImportCancelledError()); }
  cancelActive(): void { this.active?.controller.abort(new ImportCancelledError()); }

  async import(importId: unknown = randomUUID()): Promise<ImportedProblem | null> {
    const id = parseImportId(importId);
    if (this.closed) throw new Error("题面导入服务已关闭");
    if (this.active) throw new Error("题面导入正在进行，请稍候");
    const operation: ImportOperation = { id, controller: new AbortController(), status: { importId: id, phase: "selecting", progress: null } };
    this.active = operation;
    const signal = operation.controller.signal;
    const update = (phase: ImportProgress["phase"], progress: number | null) => {
      if (this.active === operation) operation.status = { importId: id, phase, progress };
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    let extraction: Promise<string> | undefined;
    try {
      // 原生选择框由用户关闭；处理期限从选定文件开始，避免催促用户选择。
      const path = await this.options.select();
      signal.throwIfAborted();
      if (!path) { update("cancelled", null); return null; }
      timer = setTimeout(() => operation.controller.abort(new ImportTimeoutError()), this.timeoutMs);
      update("reading", 0);
      const extension = extname(path).toLowerCase();
      if (![".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg"].includes(extension)) throw new Error("支持 TXT、Markdown、PDF、Word DOCX 和 PNG/JPEG 图片");
      const info = await interruptible(stat(path), signal);
      if (!info.isFile() || !info.size || info.size > MAX_FILE_BYTES) throw new Error("文件为空或超过 20MB，请拆分后导入");
      const bytes = await readFile(path, { signal });
      signal.throwIfAborted();
      if (bytes.length > MAX_FILE_BYTES) throw new Error("文件超过 20MB");
      update("extracting", 0);
      const context: ExtractionContext = { signal, onProgress: progress => {
        if (!signal.aborted && Number.isFinite(progress)) update("extracting", Math.max(operation.status.progress ?? 0, Math.min(1, Math.max(0, progress))));
      } };
      extraction = this.options.extract ? this.options.extract(extension, bytes, context)
        : extractInProcess(extension, bytes, this.options.ocrCachePath, context);
      const extracted = await interruptible(extraction, signal);
      signal.throwIfAborted();
      const problemText = extracted.replace(/^\uFEFF/, "").trim();
      if (!problemText || problemText.includes("\u0000")) throw new Error("未识别到有效文字；扫描 PDF 请改为图片导入，或手动粘贴题面");
      if (problemText.length > MAX_TEXT_CHARS) throw new Error("题面超过 6 万字，请拆分后导入");
      update("complete", 1);
      return { title: basename(path, extension).slice(0, 128), sourceName: basename(path), problemText };
    } catch (error) {
      if (signal.aborted) {
        if (signal.reason instanceof ImportCancelledError) { update("cancelled", null); return null; }
        update("timed_out", null); throw signal.reason;
      }
      update("failed", null);
      if (error && typeof error === "object" && "code" in error) throw new Error(`题面文件无法读取（${String(error.code)}）`, { cause: error });
      throw error;
    } finally {
      clearTimeout(timer);
      // 生产解析进程被结束且 exit 已确认后才释放互斥；测试解析端口允许迟到结果被丢弃。
      if (!this.options.extract) await extraction?.catch(() => {});
      if (this.active === operation) { this.last = { ...operation.status }; this.active = null; }
    }
  }
}
