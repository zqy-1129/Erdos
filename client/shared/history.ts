/** 历史查询只接受有界参数；跨端共用状态、筛选与分页口径。 */
export interface HistoryTask { taskId: string; title: string; status: string; updatedAt: string; resumable: boolean }
export type HistoryStatus = "all" | "created" | "analysis" | "modeling" | "solving" | "writing" | "done" | "resumable";
export interface HistoryQuery { query: string; status: HistoryStatus; offset: number; limit: number }
export interface HistoryPage { items: HistoryTask[]; total: number; nextOffset: number | null }
export const HISTORY_LABELS: Record<HistoryStatus, string> = { all: "全部状态", created: "尚未执行", analysis: "分析阶段",
  modeling: "建模阶段", solving: "求解阶段", writing: "写作阶段", done: "已完成", resumable: "可继续" };

export function parseHistoryQuery(value: unknown): HistoryQuery {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("历史查询参数无效");
  const raw = value as Record<string, unknown>;
  if (Object.keys(raw).some(key => !["query", "status", "offset", "limit"].includes(key))) throw new Error("历史查询参数无效");
  const query = raw.query ?? ""; const status = raw.status ?? "all";
  const offset = raw.offset ?? 0; const limit = raw.limit ?? 50;
  if (typeof query !== "string" || query.length > 128 || Array.from(query).some(c => c.charCodeAt(0) < 32) ||
      typeof status !== "string" || !Object.hasOwn(HISTORY_LABELS, status) ||
      !Number.isSafeInteger(offset) || Number(offset) < 0 || Number(offset) > 1000000 ||
      !Number.isSafeInteger(limit) || Number(limit) < 1 || Number(limit) > 100) throw new Error("历史查询参数无效");
  return { query: query.trim().toLocaleLowerCase("zh-CN"), status: status as HistoryStatus, offset: Number(offset), limit: Number(limit) };
}

/** 按已有留痕排序筛选，再分页；搜索作用于完整历史，而非仅最近 50 条。 */
export function paginateHistory(tasks: readonly HistoryTask[], query: HistoryQuery): HistoryPage {
  const filtered = tasks.filter(task => (query.status === "all" || (query.status === "resumable" ? task.resumable : task.status === query.status)) &&
    (!query.query || task.title.toLocaleLowerCase("zh-CN").includes(query.query) || task.taskId.toLocaleLowerCase("zh-CN").includes(query.query)));
  const items = filtered.slice(query.offset, query.offset + query.limit);
  const next = query.offset + items.length;
  return { items, total: filtered.length, nextOffset: next < filtered.length ? next : null };
}
