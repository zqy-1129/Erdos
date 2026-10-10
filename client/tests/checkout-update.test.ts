import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { CheckoutSession } from "../main/checkout-session.ts";
import { UpdateController } from "../main/update/controller.ts";
import { KeyVault, InMemorySecretStore, XorEncryptor } from "../main/key-vault.ts";
import type { CreatedOrder, OrderView } from "../shared/product.ts";
function orderFixture() {
  const vault = new KeyVault(new XorEncryptor(), new InMemorySecretStore());
  let account: string | null = "a"; let fail = false; let next = "created"; let id = 0; const keys: unknown[] = [];
  const product = { id: "p", code: "pack", name: "积分", type: "points_pack" as const, price_cents: 100, points: 10, duration_days: 0 };
  const order: OrderView = { id: "o", product_id: "p", channel: "alipay", status: "created", price_cents: 100,
    idempotency_key: "", expires_at: "2026-10-10T10:30:00Z", created_at: "2026-10-10T10:00:00Z" };
  const commerce = { create: async (body: Record<string, unknown>): Promise<CreatedOrder> => {
    keys.push(body.idempotency_key); if (fail) throw new Error("network");
    order.idempotency_key = String(body.idempotency_key); return { product, order: { ...order }, already_exists: keys.length > 1 };
  }, order: async (): Promise<OrderView> => ({ ...order, status: next as OrderView["status"] }) };
  const options = { vault, commerce, account: () => account, id: () => "request-" + ++id };
  return { vault, options, keys, account: (v: string | null) => { account = v; }, fail: (v: boolean) => { fail = v; }, status: (v: string) => { next = v; } };
}
describe("订单恢复", () => {
  it("POST 响应丢失后跨重启使用同一键；到账前不允许购买另一商品", async () => {
    const f = orderFixture(); let c = new CheckoutSession(f.options); f.fail(true);
    await assert.rejects(c.create({ productCode: "pack", channel: "alipay" }));
    f.fail(false); c = new CheckoutSession(f.options);
    assert.equal((await c.recover())?.status, "created"); assert.deepEqual(f.keys, ["request-1", "request-1"]);
    await assert.rejects(c.create({ productCode: "other", channel: "alipay" }), /待处理订单/);
    await c.create({ productCode: "pack", channel: "alipay" }); assert.equal(f.keys.at(-1), "request-1");
    f.status("paid"); assert.equal((await c.recover())?.status, "paid");
    await c.create({ productCode: "pack", channel: "alipay" }); assert.equal(f.keys.at(-1), "request-2");
    f.account("b"); assert.equal(await c.recover(), null);
  });
  it("登录门禁、非法请求、损坏记录与恢复错配拒绝", async () => {
    const f = orderFixture(); const c = new CheckoutSession(f.options); f.account(null);
    await assert.rejects(c.recover()); f.account("a");
    for (const body of [{}, { productCode: "../x", channel: "wechat" }, { productCode: "pack", channel: "mock" }]) await assert.rejects(c.create(body));
    await c.create({ productCode: "pack", channel: "alipay" });
    f.options.commerce.order = async () => ({ idempotency_key: "wrong" } as OrderView);
    await assert.rejects(c.recover(), /不匹配/);
    f.vault.save("client.checkout.v1", "{}"); assert.throws(() => new CheckoutSession(f.options));
  });
});
describe("受控应用更新", () => {
  function fixture(enabled = true) {
    const events = new Map<string, (...args: unknown[]) => void>(); const actions: string[] = [];
    let installable = true; let fail = false;
    const c = new UpdateController({ enabled, version: "0.1.0", canInstall: async () => installable,
      port: { on: (e, h) => events.set(e, h),
        check: async () => { actions.push("check"); if (fail) throw new Error("check failed"); },
        download: async () => { actions.push("download"); }, install: () => { actions.push("install"); } } });
    return { c, events, actions, installable: (v: boolean) => { installable = v; }, fail: (v: boolean) => { fail = v; } };
  }
  it("渠道禁用无副作用；检查下载安装顺序及运行任务门禁", async () => {
    const disabled = fixture(false); await assert.rejects(disabled.c.run("check")); assert.equal(disabled.actions.length, 0);
    const f = fixture(); await assert.rejects(f.c.run("download")); await assert.rejects(f.c.run("install"));
    await f.c.run("check"); f.events.get("update-available")!({ version: "0.2.0" });
    await f.c.run("download"); f.events.get("download-progress")!({ percent: 120 }); assert.equal(f.c.view().progress, 100);
    f.events.get("download-progress")!({ percent: NaN }); assert.equal(f.c.view().progress, 0);
    f.events.get("update-downloaded")!(); f.installable(false); await assert.rejects(f.c.run("install"), /任务正在/);
    assert.equal(f.c.view().status, "downloaded"); f.installable(true); await f.c.run("install");
    assert.deepEqual(f.actions, ["check", "download", "install"]);
    const view = f.c.view(); view.status = "error"; assert.equal(f.c.view().status, "downloaded");
  });
  it("无更新、更新失败与并发保护", async () => {
    const f = fixture(); f.events.get("update-not-available")!(); assert.equal(f.c.view().status, "current");
    f.events.get("error")!(new Error("secret path")); assert.equal(f.c.view().message.includes("secret"), false);
    f.fail(true); await assert.rejects(f.c.run("check")); f.fail(false);
    const pending = f.c.run("check"); await assert.rejects(f.c.run("check"), /正在进行/); await pending;
  });
});
