import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { describe, it } from "node:test";
import { CloudHttpClient, type FetchLike } from "../main/cloud/http.ts";
import { CloudApiError } from "../main/cloud/envelope.ts";
import { CloudCommerceClient, parseProduct, parseOrder } from "../main/cloud/commerce.ts";
import { CloudContentClient, parseContent } from "../main/cloud/content.ts";
import { CloudStageAccounting, isTransportFailure, type StageCharge } from "../main/cloud/stage-accounting.ts";
import { canonicalBytes } from "../main/entitlement/verify.ts";
import type { EntitlementService } from "../main/entitlement/service.ts";
const product = { id: "p", code: "pack", name: "积分", type: "points_pack", price_cents: 100, points: 10, duration_days: 0 };
const order = { id: "o", product_id: "p", idempotency_key: "req", channel: "alipay", status: "created", price_cents: 100, expires_at: "2026-10-10T10:30:00Z", created_at: "2026-10-10T10:00:00Z" };
const reply = (data: unknown, status = 200) => ({ status, ok: status < 400, text: async () => JSON.stringify({ code: status < 400 ? 0 : status, message: "test", data }) });
const http = (fetchImpl: FetchLike) => new CloudHttpClient({ baseUrl: "https://unit.example", maxRetries: 0, fetchImpl });
describe("商品订单和真实内容契约", () => {
  it("商品价格校验、订单幂等映射与错误形状", async () => {
    let sent: unknown; let data: unknown = [product];
    const c = new CloudCommerceClient(http(async (_url, init) => { if (init.body) sent = JSON.parse(String(init.body)); return reply(data); }));
    assert.equal((await c.products())[0].price_cents, 100);
    data = { order, product, already_exists: false };
    await c.create({ product_code: "pack", channel: "alipay", idempotency_key: "req" });
    assert.deepEqual(sent, { product_code: "pack", channel: "alipay", idempotency_key: "req" });
    data = order; assert.equal((await c.order("o")).status, "created");
    await assert.rejects(c.order("../o")); await assert.rejects(c.order("another"));
    await assert.rejects(c.create({ product_code: "pack", channel: "card", idempotency_key: "req" }));
    data = { order: { ...order, idempotency_key: "wrong" }, product, already_exists: false };
    await assert.rejects(c.create({ product_code: "pack", channel: "alipay", idempotency_key: "req" }));
    data = {}; await assert.rejects(c.products());
    for (const value of [null, [], {}, { ...product, price_cents: -1 }, { ...product, points: 1.5 }, { ...product, type: "x" }]) assert.throws(() => parseProduct(value));
    for (const value of [null, {}, { ...order, expires_at: "bad" }, { ...order, status: "fake-paid" }, { ...order, price_cents: Infinity }]) assert.throws(() => parseOrder(value));
  });
  it("列表严格使用业务索引与合规说明；授权失败无演示回退", async () => {
    const template = { business_id: "t", competition: "CUMCM", format: "latex", oss_key: "t.zip", sha256: "a".repeat(64) };
    const example = { business_id: "c", title: "案例", method_tags: ["拟合"], compliance_note: "仅供参考", oss_key: "c.pdf", sha256: "b".repeat(64) };
    const c = new CloudContentClient(http(async url => reply(url.endsWith("templates") ? [template] : [example])));
    const list = await c.list(); assert.equal(list.length, 2); assert.equal(list[1].complianceNote, "仅供参考"); assert.equal(list[1].referenceOnly, true);
    assert.equal(parseContent({ ...example, method_tags: undefined }, "case").tags.length, 0);
    for (const value of [null, {}, { ...example, sha256: "bad" }, { ...example, compliance_note: "" }, { ...example, method_tags: [1] }]) assert.throws(() => parseContent(value, "case"));
    assert.throws(() => parseContent({ ...template, format: "pdf" }, "template"));
    await assert.rejects(new CloudContentClient(http(async () => reply({}, 403))).list(), CloudApiError);
    await assert.rejects(new CloudContentClient(http(async () => reply({}))).list());
  });
});
describe("阶段签名许可与离线资金边界", () => {
  const charge: StageCharge = { execId: "exec", taskId: "task", stage: "analysis", points: 20, mode: "online" };
  function fixture() {
    const now = Date.parse("2026-10-10T10:00:00Z"); const keys = generateKeyPairSync("ed25519");
    const issued = now / 1000; const expires = issued + 600;
    const payload = { exec_id: "exec", user_id: "u", task_id: "task", stage: "analysis", points: 20, issued_at: issued, expires_at: expires };
    const grant = { exec_id: "exec", stage: "analysis", points: 20, status: "active",
      signature: sign(null, canonicalBytes(payload), keys.privateKey).toString("hex"),
      key_version: "v1", issued_at: new Date(now).toISOString(), expires_at: new Date(now + 600000).toISOString() };
    const result = { grant, balance: { user_id: "u", frozen: false } };
    const offline: string[] = []; let mode = "valid"; let key: Buffer | null = keys.publicKey.export({ type: "spki", format: "der" });
    const transport = http(async url => {
      if (url.endsWith("balance")) {
        if (mode === "offline") throw new TypeError("network");
        if (mode === "401") return reply(null, 401);
        return reply({ balance: 100 });
      }
      if (url.endsWith("reserve")) { if (mode === "write-lost") throw new TypeError("network"); return reply(result); }
      if (url.endsWith("refund") && mode === "404") return reply(null, 404);
      if (url.endsWith("refund") && mode === "500") return reply(null, 500);
      return reply({ status: mode === "bad-state" ? "other" : url.endsWith("confirm") ? "confirmed" : "refunded" });
    });
    const port = { status: () => mode === "expired" ? "grace_expired" : "ready", reserve: () => { offline.push("reserve"); }, confirm: () => { offline.push("confirm"); }, release: () => { offline.push("refund"); } } as unknown as EntitlementService;
    const c = new CloudStageAccounting(transport, port, { resolve: async () => key }, () => now);
    return { c, result, grant, offline, mode: (v: string) => { mode = v; }, key: (v: Buffer | null) => { key = v; } };
  }
  it("验签通过才启动；在线确认和退还按同一执行号", async () => {
    const f = fixture(); assert.equal(await f.c.reserve(charge), "online");
    await f.c.revalidate(charge); await f.c.confirm(charge); await f.c.refund(charge); assert.equal(f.offline.length, 0);
    f.mode("bad-state"); await assert.rejects(f.c.confirm(charge)); await assert.rejects(f.c.refund(charge));
    f.mode("404"); await f.c.refund(charge); f.mode("500"); await assert.rejects(f.c.refund(charge));
  });
  it("篡改、过期、冻结和未知公钥拒绝，不自动降级离线", async () => {
    for (const mutate of [
      (f: ReturnType<typeof fixture>) => { f.grant.signature = Buffer.alloc(64).toString("hex"); },
      (f: ReturnType<typeof fixture>) => { f.grant.exec_id = "other"; },
      (f: ReturnType<typeof fixture>) => { f.grant.expires_at = "2026-10-09T10:00:00Z"; },
      (f: ReturnType<typeof fixture>) => { f.result.balance.frozen = true; },
      (f: ReturnType<typeof fixture>) => { f.key(null); },
    ]) { const f = fixture(); mutate(f); await assert.rejects(f.c.reserve(charge)); assert.equal(f.offline.length, 0); }
  });
  it("只有预检网络故障进入离线；POST结果不明不能重复扣费", async () => {
    const f = fixture(); f.mode("offline"); assert.equal(await f.c.reserve(charge), "offline");
    await f.c.confirm({ ...charge, mode: "offline" }); await f.c.refund({ ...charge, mode: "offline" });
    assert.deepEqual(f.offline, ["reserve", "confirm", "refund"]);
    await f.c.revalidate({ ...charge, mode: "offline" }); f.mode("expired"); await assert.rejects(f.c.revalidate({ ...charge, mode: "offline" }));
    f.mode("write-lost"); await assert.rejects(f.c.reserve(charge)); assert.equal(f.offline.length, 3);
    f.mode("401"); await assert.rejects(f.c.reserve(charge)); assert.equal(f.offline.length, 3);
    assert.equal(isTransportFailure(new CloudApiError(null, 503, "down")), true);
    assert.equal(isTransportFailure(Object.assign(new Error(), { name: "AbortError" })), true);
    assert.equal(isTransportFailure(new Error("bad data")), false);
  });
});
