import type { StageName } from "./ipc.ts";

/** PRD DF-006 的阶段配额；商品价格始终来自服务端商品目录。 */
export const STAGE_POINTS: Record<StageName, number> = { analysis: 20, modeling: 30, solving: 40, writing: 30 };
export interface ProductView {
  id: string;
  code: string;
  type: "subscription" | "points_pack";
  name: string;
  price_cents: number;
  points: number;
  duration_days: number;
}
export interface OrderView {
  id: string;
  product_id: string;
  price_cents: number;
  channel: "wechat" | "alipay" | "mock";
  status: "created" | "paid" | "closed" | "refunded";
  idempotency_key: string;
  expires_at: string;
  created_at: string;
  paid_at?: string | null;
  refunded_at?: string | null;
}
export interface CreatedOrder { order: OrderView; product: ProductView; already_exists: boolean }
export interface TaskInput { task_id: string; title: string; problem_text: string }

export function parseTaskInput(value: unknown): TaskInput {
  if (!value || typeof value !== "object") throw new Error("任务题面必须为对象");
  const body = value as Record<string, unknown>;
  if (typeof body.task_id !== "string" || !/^[\w-]{1,36}$/.test(body.task_id) ||
      typeof body.title !== "string" || !body.title.trim() || body.title.length > 128 ||
      typeof body.problem_text !== "string" || !body.problem_text.trim() || body.problem_text.length > 60000) {
    throw new Error("请填写有效任务号、标题（128字以内）和题面（6万字以内）");
  }
  return { task_id: body.task_id, title: body.title.trim(), problem_text: body.problem_text.trim() };
}
