import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { describe, it } from "node:test";
import { CloudAuthBridge } from "../main/ipc/cloud-auth.ts";
import { CloudBusinessBridge } from "../main/ipc/cloud-business.ts";
import { TaskController } from "../main/task-controller.ts";
import { CheckoutSession } from "../main/checkout-session.ts";
import { InMemorySecretStore, KeyVault, XorEncryptor } from "../main/key-vault.ts";
import type { GetStatusResult, StageName } from "../shared/ipc.ts";
const baseUrl = process.env.ERDOS_CLIENT_TEST_API_URL;
describe("独立服务端真实 HTTP 产品流程", () => {
  it("注册、JWKS阶段许可、确认退款与真实商品订单", async t => {
    if (!baseUrl) { t.skip("设置 ERDOS_CLIENT_TEST_API_URL 指向独立测试服务后执行"); return; }
    const auth = new CloudAuthBridge({ baseUrl, fingerprint: randomUUID(), maxRetries: 0 });
    const account = "product-" + randomUUID() + "@example.com";
    await auth.register({ username: account, password: "UnitPass123" });
    const business = new CloudBusinessBridge({ baseUrl, getToken: () => auth.getToken(), maxRetries: 0 });
    const before = (await business.billingOverview()).pointsBalance;
    assert.equal(before, 100);
    const vault = new KeyVault(new XorEncryptor(), new InMemorySecretStore());
    const checkpoints = new Map<string, string>(); const status: GetStatusResult = { engine: "idle", task: null };
    const taskId = randomUUID();
    const options = { vault, account: () => account, hasKey: () => true, accounting: business.stageAccounting,
      checkpoint: (_task: string, stage: StageName) => checkpoints.get(stage) ?? null,
      engine: async (method: string, body: Record<string, unknown>): Promise<unknown> => {
        if (method === "get_status") return status;
        if (method === "start_stage") { status.task = { task_id: taskId, stage: body.stage as StageName, status: "running" }; return status.task; }
        if (method === "cancel") { status.task!.status = "cancelled"; return {}; }
        if (method === "answer_gate") { checkpoints.set("analysis", "done"); return { task_id: taskId, gate: "gate_analysis", decision: "pass", action: "next_stage" }; }
        return {};
      } };
    const controller = new TaskController(options);
    await controller.invoke("start_stage", { task_id: taskId, stage: "analysis" });
    assert.equal((await business.billingOverview()).pointsBalance, 80);
    status.task!.status = "done"; await controller.invoke("answer_gate", { task_id: taskId, gate: "gate_analysis", decision: "pass" });
    await controller.invoke("answer_gate", { task_id: taskId, gate: "gate_analysis", decision: "pass" });
    assert.equal((await business.billingOverview()).pointsBalance, 80);
    const restored = new TaskController(options);
    await restored.invoke("start_stage", { task_id: taskId, stage: "modeling" });
    assert.equal((await business.billingOverview()).pointsBalance, 50);
    await restored.invoke("cancel", { task_id: taskId });
    assert.equal((await business.billingOverview()).pointsBalance, 80);
    const products = await business.commerce.products(); assert.ok(products.length > 0);
    const checkout = new CheckoutSession({ vault, account: () => account, commerce: business.commerce });
    const first = await checkout.create({ productCode: products[0].code, channel: "alipay" });
    const second = await checkout.create({ productCode: products[0].code, channel: "alipay" });
    assert.equal(first.order.id, second.order.id);
    assert.equal((await new CheckoutSession({ vault, account: () => account, commerce: business.commerce }).recover())?.id, first.order.id);
    assert.equal(first.order.status, "created");
    assert.equal((await business.billingOverview()).pointsBalance, 80);
    const ledger = await business.billingLedger(); assert.ok(ledger.some(r => r.action === "confirm")); assert.ok(ledger.some(r => r.action === "refund"));
    await auth.logout();
  });
});
