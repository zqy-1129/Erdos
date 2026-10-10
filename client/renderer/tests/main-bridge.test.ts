// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
vi.mock("electron", () => ({ safeStorage: {
  isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(value.split("").reverse().join("")),
  decryptString: (value: Buffer) => value.toString().split("").reverse().join(""),
} }));
import { BridgeBackend } from "../../main/ipc/bridge.ts";
import { createPlatformEncryptor } from "../../main/safe-storage-encryptor.ts";
import { InMemorySecretStore, XorEncryptor } from "../../main/key-vault.ts";
import { BRIDGE_CHANNELS as C } from "../../shared/bridge-channels.ts";
const config = { alias: "unit", key: "unit-credential", baseUrl: "https://model.example/v1", model: "unit-model" };
const response = (data: unknown, status = 200) => ({ status, ok: status < 400, text: async () => JSON.stringify({ code: status < 400 ? 0 : status, data, message: "test" }) });
describe("主进程桥实际业务接线", () => {
  it("加密器、模型选择与偏好重启恢复、检测和未知通道", async () => {
    const encryptor = createPlatformEncryptor(); expect(encryptor.decrypt(encryptor.encrypt("unit"))).toBe("unit");
    const store = new InMemorySecretStore(); const invoke = vi.fn(async () => ({}));
    const b = new BridgeBackend({ secretStore: store, encryptor: new XorEncryptor(), engineInvoke: invoke });
    await b.handle(C.keysSave, config); expect(b.getActiveKey()).toBe(config.key);
    expect(JSON.stringify(await b.handle(C.keysList, {}))).not.toContain(config.key);
    expect((await b.handle(C.keysTest, {})) as object).toMatchObject({ reason: "unverified" });
    b.setProbe(async () => ({ ok: true, models_endpoint: true, tool_mode: "tool_loop" }));
    expect(await b.handle(C.keysTest, {})).toMatchObject({ ok: true });
    await b.handle(C.preferencesSave, { defaultModel: "other", language: "zh-CN" });
    const restored = new BridgeBackend({ secretStore: store, encryptor: new XorEncryptor(), engineInvoke: invoke });
    expect(restored.getModelEnvironment().ERDOS_MODEL_NAME).toBe("other");
    const list = await restored.handle(C.keysList, {}) as Array<{ id: string }>;
    await restored.handle(C.keysActivate, { id: list[0].id });
    await expect(restored.invokeEngine("provider_test", { base_url: "https://other", model: "unit-model" })).rejects.toThrow("已保存");
    await restored.invokeEngine("provider_test", { base_url: config.baseUrl, model: config.model });
    await restored.handle(C.keysDelete, { id: list[0].id }); expect(restored.getActiveKey()).toBeNull();
    expect(await restored.handle(C.keysUsage, {})).toMatchObject({ totalTokens: 0 });
    expect(await restored.handle(C.billingOverview, {})).toMatchObject({ pointsBalance: 93 });
    expect((await restored.handle(C.billingLedger, {})) as unknown[]).toHaveLength(120);
    expect(await restored.handle(C.billingExport, {})).toMatchObject({ canceled: false });
    expect(await restored.handle(C.billingProducts, {})).toEqual([]);
    expect((await restored.handle(C.contentList, {})) as unknown[]).toHaveLength(4);
    expect(await restored.handle(C.entitlementStatus, {})).toMatchObject({ status: "ready" });
    restored.captureTelemetry("page_view", { path: "unit" });
    for (const channel of [C.taskImport, C.historyList, C.historyResume, C.complianceExport, C.complianceSave, C.trailRecentTasks, C.artifactsList, C.updateStatus, C.updateCheck, C.authResetRequest, C.authResetConfirm, "unknown"]) {
      await expect(restored.handle(channel, {})).rejects.toThrow();
    }
    await expect(restored.handle(C.keysList, "bad")).rejects.toThrow("对象");
    await expect(restored.invokeEngine("get_status", null as any)).rejects.toThrow("对象");
    await expect(new BridgeBackend({ encryptor: new XorEncryptor() }).invokeEngine("get_status", {})).rejects.toThrow("未接线");
  });
  it("cloud 登录、购买恢复、真实内容与重置吊销；不采用 mock 支付", async () => {
    let pending: any = null; const invalidated = vi.fn();
    const product = { id: "p", code: "pack", type: "points_pack", name: "积分", price_cents: 100, points: 10, duration_days: 0 };
    const calls: string[] = [];
    const b = new BridgeBackend({ secretStore: new InMemorySecretStore(), encryptor: new XorEncryptor(), onSessionInvalidated: invalidated,
      auth: { runtime: { mode: "cloud", baseUrl: "https://cloud.example" }, fingerprint: "unit-fingerprint",
        fetchImpl: async (url, init) => {
          calls.push(url); const body = init.body ? JSON.parse(String(init.body)) : {};
          if (url.endsWith("/login") || url.endsWith("/register")) return response({ access_token: "unit-access", refresh_token: "unit-refresh", token_type: "Bearer", expires_in: 900, refresh_expires_in: 3600 });
          if (url.includes("/reset/")) return response({ accepted: true });
          if (url.endsWith("/products")) return response([product]);
          if (url.endsWith("/orders")) {
            pending = { id: "o", product_id: "p", channel: body.channel, status: "created", price_cents: 100,
              idempotency_key: body.idempotency_key, created_at: new Date().toISOString(), expires_at: new Date(Date.now()+1800000).toISOString() };
            return response({ product, order: pending, already_exists: false });
          }
          if (url.endsWith("/orders/o")) return response(pending);
          if (url.includes("/content/")) return response([]);
          return response({ revoked: 1 });
        } } });
    await expect(b.handle(C.billingProducts, {})).rejects.toThrow("登录");
    await b.handle(C.authLogin, { username: "unit@example.com", password: "unit-password" });
    expect(await b.handle(C.authSession, {})).toMatchObject({ username: "unit@example.com" });
    expect(await b.handle(C.billingProducts, {})).toHaveLength(1);
    await expect(b.handle(C.billingCreateOrder, { productCode: "pack", channel: "mock" })).rejects.toThrow("支付方式");
    expect(await b.handle(C.billingCreateOrder, { productCode: "pack", channel: "alipay" })).toMatchObject({ order: { status: "created" } });
    pending.status = "paid"; expect(await b.handle(C.billingPendingOrder, {})).toMatchObject({ status: "paid" });
    expect(await b.handle(C.billingOrder, { orderId: "o" })).toMatchObject({ status: "paid" });
    expect(await b.handle(C.contentList, {})).toEqual([]);
    await b.handle(C.authResetRequest, { identifier: "unit@example.com" });
    await expect(b.handle(C.authResetConfirm, { token: "short", password: "unit" })).rejects.toThrow();
    await b.handle(C.authResetConfirm, { token: "unit-reset-token-123", password: "unit-password" });
    expect(invalidated).toHaveBeenCalledTimes(1); expect(await b.handle(C.authSession, {})).toBeNull();
    await expect(b.handle(C.contentList, {})).rejects.toThrow("登录");
    await b.handle(C.authRegister, { username: "unit@example.com", password: "unit-pass123" });
    await b.handle(C.authLogout, {}); expect(await b.handle(C.authSession, {})).toBeNull();
    expect(calls.some(url => url.includes("/password/reset/confirm"))).toBe(true);
  });
  it("生产未配置与持久化失效拒绝；执行中禁止检测和换模型", async () => {
    const b = new BridgeBackend({ encryptor: new XorEncryptor(), auth: { runtime: { mode: "unconfigured" } } });
    for (const c of [C.authLogin, C.authRegister, C.entitlementStatus, C.billingOverview, C.billingLedger, C.billingExport, C.billingProducts]) await expect(b.handle(c, {})).rejects.toThrow();
    const busy = new BridgeBackend({ encryptor: new XorEncryptor(), modelBusy: () => true });
    await expect(busy.handle(C.keysSave, config)).rejects.toThrow("执行中");
    expect(() => new BridgeBackend({ encryptor: new XorEncryptor(), secretStore: null, auth: { runtime: { mode: "cloud", baseUrl: "https://unit.example" }, fingerprint: "unit-fingerprint" } })).toThrow("持久化");
    const reload = vi.fn(async () => {}); const engine = vi.fn(async () => ({}));
    const fresh = new BridgeBackend({ encryptor: new XorEncryptor(), reloadModel: reload, engineInvoke: engine });
    await fresh.handle(C.keysSave, config); await fresh.invokeEngine("task_create", {});
    expect(reload).toHaveBeenCalledTimes(1);
    await fresh.invokeEngine("task_create", {}); expect(reload).toHaveBeenCalledTimes(1);
  });
});
