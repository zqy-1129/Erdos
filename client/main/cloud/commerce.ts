import type { CreatedOrder, OrderView, ProductView } from "../../shared/product.ts";
import { CloudHttpClient } from "./http.ts";

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("商品或订单响应形状无效");
  return value as Record<string, unknown>;
}
export function parseProduct(value: unknown): ProductView {
  const data = record(value);
  if (!["id", "code", "name"].every(k => typeof data[k] === "string" && data[k]) ||
      !["subscription", "points_pack"].includes(String(data.type)) ||
      !["price_cents", "points", "duration_days"].every(k => Number.isSafeInteger(data[k]) && Number(data[k]) >= 0)) {
    throw new Error("商品响应形状无效");
  }
  return { id: String(data.id), code: String(data.code), name: String(data.name), type: data.type as ProductView["type"],
    price_cents: Number(data.price_cents), points: Number(data.points), duration_days: Number(data.duration_days) };
}
export function parseOrder(value: unknown): OrderView {
  const data = record(value);
  if (!["id", "product_id", "idempotency_key"].every(k => typeof data[k] === "string" && data[k]) ||
      !["wechat", "alipay", "mock"].includes(String(data.channel)) ||
      !["created", "paid", "closed", "refunded"].includes(String(data.status)) ||
      !Number.isSafeInteger(data.price_cents) || Number(data.price_cents) < 0 ||
      !["expires_at", "created_at"].every(k => typeof data[k] === "string" && Number.isFinite(Date.parse(String(data[k]))))) {
    throw new Error("订单响应形状无效");
  }
  return { id: String(data.id), product_id: String(data.product_id), idempotency_key: String(data.idempotency_key),
    channel: data.channel as OrderView["channel"], status: data.status as OrderView["status"],
    price_cents: Number(data.price_cents), expires_at: String(data.expires_at), created_at: String(data.created_at) };
}

/** 购买请求使用调用方持有的幂等键；订单只接受服务端状态，不在客户端模拟到账。 */
export class CloudCommerceClient {
  private readonly http: CloudHttpClient;
  constructor(http: CloudHttpClient) { this.http = http; }
  async products(): Promise<ProductView[]> {
    const raw = await this.http.get<unknown>("/v1/billing/products");
    if (!Array.isArray(raw)) throw new Error("商品目录响应形状无效");
    return raw.map(parseProduct);
  }
  async create(body: Record<string, unknown>): Promise<CreatedOrder> {
    if (typeof body.product_code !== "string" || !body.product_code.trim() || body.product_code.length > 32 ||
        typeof body.idempotency_key !== "string" || !/^[\w-]{1,64}$/.test(body.idempotency_key) ||
        !["wechat", "alipay", "mock"].includes(String(body.channel))) throw new Error("请选择商品、支付方式并提供有效订单请求号");
    const raw = record(await this.http.post("/v1/billing/orders", {
      product_code: body.product_code, channel: body.channel, idempotency_key: body.idempotency_key,
    }));
    const order = parseOrder(raw.order);
    const product = parseProduct(raw.product);
    if (order.idempotency_key !== body.idempotency_key || product.code !== body.product_code ||
        order.product_id !== product.id || order.channel !== body.channel || typeof raw.already_exists !== "boolean") {
      throw new Error("订单与购买请求不一致");
    }
    return { order, product, already_exists: raw.already_exists };
  }
  async order(id: unknown): Promise<OrderView> {
    if (typeof id !== "string" || !/^[\w-]{1,64}$/.test(id)) throw new Error("订单号无效");
    const order = parseOrder(await this.http.get(`/v1/billing/orders/${encodeURIComponent(id)}`));
    if (order.id !== id) throw new Error("订单号与响应不一致");
    return order;
  }
}
