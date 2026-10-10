import { randomUUID } from "node:crypto";
import type { CreatedOrder, OrderView } from "../shared/product.ts";
import type { CloudCommerceClient } from "./cloud/commerce.ts";
import type { KeyVault } from "./key-vault.ts";

interface Pending { account: string; productCode: string; channel: "wechat" | "alipay"; idempotencyKey: string; order: OrderView | null }
export interface CheckoutOptions { commerce: Pick<CloudCommerceClient, "create" | "order">; vault: KeyVault; account: () => string | null; id?: () => string }
const SCOPE = "client.checkout.v1";

/** 下单前加密保存幂等键；网络重试与重启恢复都使用同一订单，绝不在客户端模拟到账。 */
export class CheckoutSession {
  private readonly options: CheckoutOptions;
  private pending: Pending[];
  private tail: Promise<unknown> = Promise.resolve();
  constructor(options: CheckoutOptions) {
    this.options = options;
    try {
      this.pending = JSON.parse(options.vault.load(SCOPE) ?? "[]") as Pending[];
      if (!Array.isArray(this.pending) || !this.pending.every(p => p && typeof p.account === "string" &&
          typeof p.productCode === "string" && ["wechat", "alipay"].includes(p.channel) &&
          /^[\w-]{1,64}$/.test(p.idempotencyKey) && (p.order === null || typeof p.order.id === "string"))) throw new Error();
    } catch { throw new Error("本地订单恢复记录损坏，请核对云端订单后处理"); }
  }
  private serial<T>(operation: () => Promise<T>): Promise<T> {
    const work = this.tail.then(operation);
    this.tail = work.catch(() => undefined);
    return work;
  }
  private account(): string {
    const account = this.options.account();
    if (!account) throw new Error("请先登录后购买");
    return account;
  }
  private save(pending: Pending): void {
    const next = this.pending.filter(p => p.account !== pending.account);
    next.push(pending);
    this.options.vault.save(SCOPE, JSON.stringify(next));
    this.pending = next;
  }
  create(body: Record<string, unknown>): Promise<CreatedOrder> {
    return this.serial(async () => {
      const account = this.account();
      if (typeof body.productCode !== "string" || !/^[\w-]{1,32}$/.test(body.productCode) ||
          !["wechat", "alipay"].includes(String(body.channel))) throw new Error("请选择有效商品和支付方式");
      let intent = this.pending.find(p => p.account === account);
      if (intent && (!intent.order || intent.order.status === "created")) {
        if (intent.productCode !== body.productCode || intent.channel !== body.channel) throw new Error("已有待处理订单，请先恢复并查询该订单");
      } else intent = undefined;
      intent ??= { account, productCode: body.productCode, channel: body.channel as Pending["channel"],
        idempotencyKey: (this.options.id ?? randomUUID)(), order: null };
      this.save(intent);
      const created = await this.options.commerce.create({ product_code: intent.productCode, channel: intent.channel,
        idempotency_key: intent.idempotencyKey });
      this.save({ ...intent, order: created.order });
      return created;
    });
  }
  recover(): Promise<OrderView | null> {
    return this.serial(async () => {
      const account = this.account();
      const intent = this.pending.find(p => p.account === account);
      if (!intent) return null;
      const order = intent.order ? await this.options.commerce.order(intent.order.id) :
        (await this.options.commerce.create({ product_code: intent.productCode, channel: intent.channel,
          idempotency_key: intent.idempotencyKey })).order;
      if (order.idempotency_key !== intent.idempotencyKey) throw new Error("订单恢复响应不匹配");
      this.save({ ...intent, order });
      return order;
    });
  }
}
