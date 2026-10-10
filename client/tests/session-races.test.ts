import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { AuthClient, InMemoryTokenStore, SessionChangedError, SessionTokenProvider, type StoredSession, type TokenPair, type TokenStore } from "../main/cloud/auth-client.ts";
import { CloudHttpClient } from "../main/cloud/http.ts";
import { CloudAuthBridge } from "../main/ipc/cloud-auth.ts";
import { isTransportFailure } from "../main/cloud/stage-accounting.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}
const pair = (name: string): TokenPair => ({ access_token: "fixture-access-" + name, refresh_token: "fixture-refresh-" + name,
  token_type: "Bearer", expires_in: 60, refresh_expires_in: 86400 });
const response = (data: unknown, status = 200) => ({ status, ok: status === 200,
  text: async () => JSON.stringify({ code: status === 200 ? 0 : 40101, data, message: "fixture" }) });
function fixture(store: TokenStore = new InMemoryTokenStore()) {
  let now = 0; let refreshes = 0; let revocations = 0;
  const gates = [deferred<ReturnType<typeof response>>(), deferred<ReturnType<typeof response>>()];
  const fetchImpl = async (url: string) => {
    if (url.endsWith("/refresh")) return gates[refreshes++].promise;
    if (url.endsWith("/logout")) { revocations++; return response({ revoked: 1 }); }
    return response(pair("bob"));
  };
  const provider = new SessionTokenProvider({ auth: new AuthClient(new CloudHttpClient({ baseUrl: "https://fixture.example", fetchImpl, maxRetries: 0 })),
    clock: () => now, store });
  provider.adopt(pair("alice"), "alice");
  return { provider, store, gates, advance: () => { now += 60000; }, refreshes: () => refreshes, revocations: () => revocations };
}
describe("会话并发与持久化失败", () => {
  it("已发送请求的迟到401和成功响应均被取消，不清除或覆盖新会话", async () => {
    for (const status of [200, 401, 403]) {
      const f = fixture(); const gate = deferred<ReturnType<typeof response>>(); let invalidations = 0;
      const http = new CloudHttpClient({ baseUrl: "https://fixture.example", getToken: () => f.provider.getToken(),
        getSessionRevision: () => f.provider.sessionRevision(), onUnauthorized: () => { invalidations++; f.provider.clearSession(); },
        fetchImpl: async () => gate.promise });
      const rejected = assert.rejects(http.get("/v1/points/balance"), SessionChangedError);
      await Promise.resolve(); await f.provider.login("bob", "fixture-password");
      gate.resolve(response({ user_id: "alice" }, status)); await rejected;
      assert.equal(invalidations, 0); assert.equal(f.provider.currentSession()?.username, "bob");
    }
  });
  it("网络重试等待中切换账号后停止重试，不发送新账号令牌", async () => {
    const f = fixture(); const gate = deferred<ReturnType<typeof response>>(); let calls = 0;
    const http = new CloudHttpClient({ baseUrl: "https://fixture.example", getToken: () => f.provider.getToken(),
      getSessionRevision: () => f.provider.sessionRevision(), retryBackoffMs: 50,
      fetchImpl: async () => { calls++; return gate.promise; } });
    const rejected = assert.rejects(http.get("/v1/points/balance"), SessionChangedError);
    await Promise.resolve(); gate.resolve(response(null, 503)); await new Promise<void>(resolve => setImmediate(resolve));
    await f.provider.login("bob", "fixture-password"); await rejected; assert.equal(calls, 1);
  });
  it("读取响应体期间退出会取消结果；正常令牌轮换不改变账号会话版本", async () => {
    const f = fixture(); const body = deferred<string>(); const readStarted = deferred<void>();
    const http = new CloudHttpClient({ baseUrl: "https://fixture.example", getToken: () => f.provider.getToken(),
      getSessionRevision: () => f.provider.sessionRevision(), fetchImpl: async () => ({ status: 200, ok: true,
        text: () => { readStarted.resolve(); return body.promise; } }) });
    const rejected = assert.rejects(http.get("/v1/points/balance"), SessionChangedError);
    await readStarted.promise; await f.provider.logout(); body.resolve(JSON.stringify({ code: 0, data: {} })); await rejected;
    const fresh = fixture(); const revision = fresh.provider.sessionRevision(); fresh.advance();
    const rotated = fresh.provider.getToken(); fresh.gates[0].resolve(response(pair("rotated"))); await rotated;
    assert.equal(fresh.provider.sessionRevision(), revision);
  });
  it("最后一次网络失败遇到会话切换仍报取消，不能降级离线", async () => {
    const f = fixture(); const gate = deferred<void>();
    const http = new CloudHttpClient({ baseUrl: "https://fixture.example", getToken: () => f.provider.getToken(),
      getSessionRevision: () => f.provider.sessionRevision(), maxRetries: 0,
      fetchImpl: async () => { await gate.promise; throw new TypeError("fixture network failure"); } });
    const rejected = assert.rejects(http.get("/v1/points/balance"), error => {
      assert.ok(error instanceof SessionChangedError); assert.equal(isTransportFailure(error), false); return true;
    });
    await Promise.resolve(); await f.provider.logout(); gate.resolve(); await rejected;
  });
  it("HTTP通道不重试会话取消，不使用新账号发送旧请求或降级离线记账", async () => {
    const f = fixture(); f.advance(); let sent = 0;
    const http = new CloudHttpClient({ baseUrl: "https://fixture.example", getToken: () => f.provider.getToken(),
      maxRetries: 2, retryBackoffMs: 0, fetchImpl: async () => { sent++; return response({}); } });
    const rejected = assert.rejects(http.post("/v1/points/reserve", { exec_id: "old-request" }), error => {
      assert.ok(error instanceof SessionChangedError); assert.equal(error.httpStatus, 409);
      assert.equal(isTransportFailure(error), false); return true;
    });
    await f.provider.login("bob", "fixture-password"); f.gates[0].resolve(response(pair("old"))); await rejected;
    assert.equal(sent, 0); assert.equal(f.provider.currentSession()?.username, "bob");
  });
  it("退出后迟到刷新成功不能恢复内存或加密存储中的会话", async () => {
    const f = fixture(); f.advance(); const rejected = assert.rejects(f.provider.getToken(), /会话已变更/);
    await Promise.resolve(); assert.equal(await f.provider.logout(), 1);
    f.gates[0].resolve(response(pair("old"))); await rejected;
    assert.equal(f.provider.signedIn(), false); assert.equal(f.store.load(), null); assert.equal(await f.provider.getToken(), null);
  });
  it("旧会话刷新401不能清掉新登录，旧请求也不能改用新用户令牌", async () => {
    const f = fixture(); f.advance(); const rejected = assert.rejects(f.provider.getToken(), /会话已变更/);
    await f.provider.login("bob", "fixture-password");
    f.gates[0].resolve(response(null, 401)); await rejected;
    assert.equal(f.provider.currentSession()?.username, "bob"); assert.equal(await f.provider.getToken(), pair("bob").access_token);
  });
  it("旧刷新结束不会清空新刷新去重锁，当前会话仍只刷新一次", async () => {
    const f = fixture(); f.advance(); const oldRejected = assert.rejects(f.provider.getToken(), /会话已变更/);
    await f.provider.login("bob", "fixture-password"); f.advance();
    const fresh = f.provider.getToken();
    f.gates[0].resolve(response(pair("old"))); await oldRejected;
    assert.equal(f.provider.getToken(), fresh); assert.equal(f.refreshes(), 2);
    f.gates[1].resolve(response(pair("fresh"))); assert.equal(await fresh, pair("fresh").access_token);
  });
  it("迟到登录与注册在退出或新登录后被取消，不覆盖当前会话", async () => {
    const login = deferred<ReturnType<typeof response>>(); const registration = deferred<ReturnType<typeof response>>();
    const fetchImpl = async (url: string, init: { body?: unknown }) => {
      if (url.endsWith("/register")) return registration.promise;
      const body = JSON.parse(String(init.body));
      return body.username === "alice" ? login.promise : response(pair("bob"));
    };
    const bridge = new CloudAuthBridge({ baseUrl: "https://fixture.example", fingerprint: "a".repeat(64), fetchImpl, maxRetries: 0 });
    const oldLogin = assert.rejects(bridge.login({ username: "alice", password: "fixture-password" }), /会话已变更/);
    bridge.clearSession(); await bridge.login({ username: "bob", password: "fixture-password" });
    login.resolve(response(pair("alice"))); await oldLogin; assert.equal(bridge.currentView()?.username, "bob");
    const oldRegister = assert.rejects(bridge.register({ username: "alice@example.com", password: "Fixture123" }), /会话已变更/);
    bridge.clearSession(); registration.resolve(response(pair("registered"))); await oldRegister;
    assert.equal(bridge.signedIn(), false);
  });
  it("登录写失败不发布未持久化会话；退出写失败仍发起服务端撤销", async () => {
    let saved: StoredSession | null = null; let fail = false;
    const store = { load: () => saved, save: (value: StoredSession | null) => { if (fail) throw new Error("fixture storage failed"); saved = value; } };
    const f = fixture(store); f.provider.clearSession(); fail = true;
    await assert.rejects(f.provider.login("bob", "fixture-password"), /storage failed/); assert.equal(f.provider.signedIn(), false);
    fail = false; f.provider.adopt(pair("alice"), "alice"); fail = true;
    await assert.rejects(f.provider.logout(), /storage failed/); assert.equal(f.provider.signedIn(), false); assert.equal(f.revocations(), 1);
  });
});
