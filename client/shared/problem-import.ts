/** 导入状态只含操作标识与进度，不暴露用户文件路径、题面或解析器日志。 */
export type ImportPhase = "selecting" | "reading" | "extracting" | "complete" | "cancelled" | "failed" | "timed_out";
export interface ImportProgress { importId: string; phase: ImportPhase; progress: number | null }
export interface ImportedProblem { title: string; problemText: string; sourceName: string }

export function parseImportId(value: unknown): string {
  if (typeof value !== "string" || !/^[a-zA-Z0-9-]{8,64}$/.test(value)) throw new Error("导入操作标识无效");
  return value;
}
