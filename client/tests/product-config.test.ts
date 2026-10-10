import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { CredentialRegistry, validateModelEndpoint } from "../main/credential-registry.ts";
import { KeyVault, InMemorySecretStore, XorEncryptor } from "../main/key-vault.ts";
import { PreferencesStore } from "../main/preferences-store.ts";
import { parsePreferences } from "../shared/preferences.ts";
import { parseTaskInput } from "../shared/product.ts";
import { assertBridgeChannel } from "../shared/preload-policy.ts";
import { maskText, containsSecret } from "../main/secret-masker.ts";
import { BRIDGE_CHANNELS } from "../shared/bridge-channels.ts";
import { ENGINE_IPC_CHANNELS } from "../shared/ipc.ts";
const config = { alias: "配置", baseUrl: "https://model.example/v1", model: "unit-model", key: "unit-only-credential" };
describe("持久模型配置和偏好", () => {
  it("点分段 Key 在错误与日志中完整脱敏", () => {
    const synthetic = "sk-test-segment.part-a.part-b-123456789";
    const masked = maskText("error " + synthetic);
    assert.equal(masked.includes(synthetic), false);
    assert.equal(masked.includes("part-a"), false);
    assert.equal(containsSecret(masked), false);
  });
  it("加密整包、跨重启恢复、视图脱敏、激活删除", () => {
    const store = new InMemorySecretStore(); const vault = new KeyVault(new XorEncryptor(), store);
    let n = 0; const registry = new CredentialRegistry(vault, () => "id-" + ++n);
    const first = registry.save(config); const second = registry.save({ ...config, alias: "第二配置" });
    assert.equal(registry.active()?.id, second.id);
    registry.activate(first.id); registry.mark("ok");
    const restored = new CredentialRegistry(vault);
    assert.equal(restored.active()?.key, config.key); assert.equal(restored.list()[0].status, "ok");
    assert.equal(JSON.stringify(restored.list()).includes(config.key), false);
    const copy = restored.active()!; copy.key = "changed"; assert.equal(restored.active()?.key, config.key);
    assert.deepEqual(restored.remove("absent"), { ok: false, requeue: false });
    assert.deepEqual(restored.remove(second.id), { ok: true, requeue: false });
    assert.deepEqual(restored.remove(first.id), { ok: true, requeue: true });
    assert.equal(restored.active(), null); assert.throws(() => restored.activate("absent"));
    const prefs = new PreferencesStore(store); assert.equal(prefs.load().defaultModel, "");
    prefs.save({ defaultModel: " model-b ", language: "zh-CN" });
    assert.equal(new PreferencesStore(store).load().defaultModel, "model-b");
    store.set("client.preferences.v1", "broken"); assert.throws(() => prefs.load());
  });
  it("非法 URL、控制字符、超长配置和损坏存储拒绝", () => {
    for (const url of ["", "abc", "http://remote.example", "https://u:p@host/v1", "https://host?key=secret", "https://host/#x", "a".repeat(2049), 1]) assert.throws(() => validateModelEndpoint(url));
    for (const url of ["http://localhost:123/v1/", "http://127.0.0.1/v1", "http://[::1]/v1"]) assert.ok(validateModelEndpoint(url));
    const vault = new KeyVault(new XorEncryptor(), new InMemorySecretStore());
    const registry = new CredentialRegistry(vault);
    for (const field of ["key", "model"]) for (const value of ["", "a\n", "a\0", "x".repeat(5000)]) assert.throws(() => registry.save({ ...config, [field]: value }));
    assert.throws(() => registry.save({ ...config, alias: "x".repeat(65) }));
    assert.equal(registry.save({ ...config, alias: "" }).ok, true);
    vault.save("client.credentials.v1", JSON.stringify({ activeId: "absent", entries: [] }));
    assert.throws(() => new CredentialRegistry(vault), /无法解密/);
    vault.save("client.credentials.v1", "bad"); assert.throws(() => new CredentialRegistry(vault));
    for (const body of [null, [], { language: "en-US", defaultModel: "" }, { language: "zh-CN", defaultModel: "a\n" }, { language: "zh-CN", defaultModel: "a\0" }]) assert.throws(() => parsePreferences(body));
    assert.equal(parsePreferences({ language: "zh-CN", defaultModel: "" }).defaultModel, "");
  });
  it("配置上限和写入失败不会改变内存", () => {
    const store = new InMemorySecretStore(); const vault = new KeyVault(new XorEncryptor(), store);
    const registry = new CredentialRegistry(vault);
    for (let i = 0; i < 100; i++) registry.save(config);
    assert.throws(() => registry.save(config), /上限/);
    const before = registry.active()?.id;
    store.set = () => { throw new Error("disk full"); };
    assert.throws(() => registry.remove(before!)); assert.equal(registry.active()?.id, before);
  });
});
describe("任务输入和运行时 IPC 白名单", () => {
  it("题面边界对齐 60000 字并拒绝空白和错误类型", () => {
    const input = { task_id: "t-1", title: "标题", problem_text: "x".repeat(60000) };
    assert.equal(parseTaskInput(input).problem_text.length, 60000);
    for (const body of [null, {}, { ...input, task_id: "../x" }, { ...input, title: " " }, { ...input, problem_text: " " }, { ...input, problem_text: "x".repeat(60001) }]) assert.throws(() => parseTaskInput(body));
  });
  it("每个业务通道只能按正确方向访问，任意通道拒绝", () => {
    for (const channel of new Set([...Object.values(BRIDGE_CHANNELS), ...ENGINE_IPC_CHANNELS])) {
      const event = ["engine:event", "auth:session-invalidated"].includes(channel);
      assertBridgeChannel(channel, event ? "subscribe" : "invoke");
      assert.throws(() => assertBridgeChannel(channel, event ? "invoke" : "subscribe"));
    }
    assert.throws(() => assertBridgeChannel("node:readFile", "invoke"));
    assert.throws(() => assertBridgeChannel("", "subscribe"));
  });
});
