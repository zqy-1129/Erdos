/**
 * 云端正版鉴权接线测试（SP3-5）：运行模式解析、注册载荷映射、错误分类归因、
 * CloudAuthBridge 全链路（fetch 注入桩，不发真实网络请求）。
 * 真实服务端联调见 tests/sp3-5-cloud-live.test.ts。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AuthFailureError,
  CloudAuthBridge,
  buildRegisterBody,
  classifyAuthError,
  resolveAuthRuntime,
  sessionViewOf,
} from "../main/ipc/cloud-auth.ts";
import { CloudApiError } from "../main/cloud/envelope.ts";
import type { FetchLike } from "../main/cloud/http.ts";
import { InMemoryTokenStore, type TokenPair } from "../main/cloud/auth-client.ts";
import { SESSION_SCOPE, createEncryptedTokenStore } from "../main/cloud/session-store.ts";
import { InMemorySecretStore, XorEncryptor } from "../main/key-vault.ts";

/** 服务端统一信封（code=0 成功）。 */
function envelope(data: unknown): string {
  return JSON.stringify({ code: 0, message: "ok", detail: null, data });
}

function jsonResponse(status: number, body: string): Pick<Response, "status" | "ok" | "text"> {
  return { status, ok: status >= 200 && status < 300, text: async () => body };
}

const PAIR: TokenPair = {
  access_token: "at-1",
  refresh_token: "rt-1",
  token_type: "Bearer",
  expires_in: 900,
  refresh_expires_in: 86_400,
};

/** 会话签发时刻基准（恢复视图的剩余有效期断言用）。 */
const BASE_TOKEN_MS = 1_767_744_000_000;

interface CapturedRequest {
  url: string;
  body: Record<string, unknown>;
}

/** 记录请求并按路径应答的 fetch 桩（未注册路径直接抛错，暴露意外请求）。 */
function stubFetch(
  routes: Record<string, { status: number; body: string }>,
  captured: CapturedRequest[],
): FetchLike {
  return async (input, init) => {
    const url = String(input);
    captured.push({
      url,
      body: init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {},
    });
    const route = routes[new URL(url).pathname];
    if (!route) throw new Error(`未预期请求：${url}`);
    return jsonResponse(route.status, route.body);
  };
}

function makeBridge(options: {
  routes: Record<string, { status: number; body: string }>;
  captured: CapturedRequest[];
  platform?: string;
}): CloudAuthBridge {
  return new CloudAuthBridge({
    baseUrl: "http://stub",
    fingerprint: "fp-12345678",
    platform: options.platform,
    maxRetries: 0,
    fetchImpl: stubFetch(options.routes, options.captured),
  });
}

describe("resolveAuthRuntime 运行模式解析", () => {
  it("配置服务地址 → cloud（去空白与尾斜杠）", () => {
    assert.deepEqual(resolveAuthRuntime({ apiBaseUrl: " http://127.0.0.1:8000/ ", dev: false }), {
      mode: "cloud",
      baseUrl: "http://127.0.0.1:8000",
    });
  });

  it("开发期未配置 → demo 演示回退", () => {
    assert.deepEqual(resolveAuthRuntime({ apiBaseUrl: undefined, dev: true }), { mode: "demo" });
    assert.deepEqual(resolveAuthRuntime({ apiBaseUrl: "   ", dev: true }), { mode: "demo" });
  });

  it("非开发期未配置 → unconfigured（fail-closed）", () => {
    assert.deepEqual(resolveAuthRuntime({ apiBaseUrl: null, dev: false }), { mode: "unconfigured" });
  });
});

describe("会话视图与错误分类", () => {
  it("会话视图：expires_in 秒 → ms", () => {
    assert.deepEqual(sessionViewOf(PAIR, "alice"), { username: "alice", expiresInMs: 900_000 });
    assert.deepEqual(sessionViewOf({ ...PAIR, expires_in: 0 }, "alice").expiresInMs, 0);
  });

  it("HTTP 层错误按状态归类（401 凭证 / 403 冻结 / 5xx 不可用 / 0 网络）", () => {
    assert.equal(classifyAuthError(new CloudApiError(null, 401, "未授权（HTTP 401）")).reason, "credentials");
    assert.equal(classifyAuthError(new CloudApiError(null, 403, "未授权（HTTP 403）")).reason, "frozen");
    assert.equal(classifyAuthError(new CloudApiError(null, 500, "服务内部错误")).reason, "unavailable");
    assert.equal(classifyAuthError(new CloudApiError(0, 0, "云端请求失败：fetch failed")).reason, "network");
  });

  it("业务错误码归类（400 参数 / 409 冲突 / 423 锁定，均为透传信封的响应）", () => {
    const invalid = classifyAuthError(new CloudApiError(40001, 400, "请求参数错误：密码必须同时包含字母与数字"));
    assert.equal(invalid.reason, "invalid");
    assert.match(invalid.message, /密码必须同时包含字母与数字/);
    assert.equal(classifyAuthError(new CloudApiError(40901, 409, "资源状态冲突")).reason, "conflict");
    assert.equal(classifyAuthError(new CloudApiError(42301, 423, "登录尝试过多")).reason, "locked");
  });

  it("未映射业务码（如 42901 限流）→ unknown 且透传服务端可读文案", () => {
    const limited = classifyAuthError(new CloudApiError(42901, 429, "请求过于频繁，请稍后重试"));
    assert.equal(limited.reason, "unknown");
    assert.equal(limited.message, "请求过于频繁，请稍后重试");
  });

  it("非云端错误与已归因错误：透传不重复包装", () => {
    const unknown = classifyAuthError(new TypeError("fetch failed"));
    assert.equal(unknown.reason, "unknown");
    assert.equal(unknown.message, "fetch failed");
    const wrapped = new AuthFailureError("locked", "已锁定");
    assert.equal(classifyAuthError(wrapped), wrapped);
    assert.equal(classifyAuthError("字符串错误").reason, "unknown");
  });
});

describe("buildRegisterBody 注册载荷映射", () => {
  it("邮箱账号 → email 字段（契约字段集，不含 device_id）", () => {
    assert.deepEqual(buildRegisterBody({ username: " a@b.com ", password: "Passw0rd1", fingerprint: "fp-12345678" }), {
      ok: true,
      body: { email: "a@b.com", password: "Passw0rd1", fingerprint: "fp-12345678" },
    });
  });

  it("手机号账号 → phone 字段；platform 透传", () => {
    assert.deepEqual(
      buildRegisterBody({ username: "13800000000", password: "Passw0rd1", fingerprint: "fp-12345678", platform: "win32" }),
      {
        ok: true,
        body: { phone: "13800000000", password: "Passw0rd1", fingerprint: "fp-12345678", platform: "win32" },
      },
    );
  });

  it("本地校验失败：空账号 / 超长邮箱(>255) / 超长手机号(>32) / 短密码 / 非法指纹", () => {
    const cases = [
      { username: "  ", password: "Passw0rd1", fingerprint: "fp-12345678" },
      { username: `${"a".repeat(250)}@b.com`, password: "Passw0rd1", fingerprint: "fp-12345678" },
      { username: "1".repeat(33), password: "Passw0rd1", fingerprint: "fp-12345678" },
      { username: "a@b.com", password: "short7", fingerprint: "fp-12345678" },
      { username: "a@b.com", password: "Passw0rd1", fingerprint: "short" },
    ];
    for (const input of cases) {
      const built = buildRegisterBody(input);
      assert.equal(built.ok, false, JSON.stringify(input));
    }
  });
});

describe("CloudAuthBridge 云链路（fetch 桩）", () => {
  it("登录成功：请求契约路径并登记会话（令牌可读取）", async () => {
    const captured: CapturedRequest[] = [];
    const bridge = makeBridge({ routes: { "/v1/auth/login": { status: 200, body: envelope(PAIR) } }, captured });
    const view = await bridge.login({ username: " alice ", password: "secret" });
    assert.deepEqual(view, { username: "alice", expiresInMs: 900_000 });
    assert.equal(bridge.signedIn(), true);
    assert.equal(captured[0]?.url, "http://stub/v1/auth/login");
    assert.deepEqual(captured[0]?.body, { username: "alice", password: "secret" });
    assert.equal(await bridge.getToken(), "at-1");
  });

  it("注册成功：请求 RegisterCreate 字段集（邮箱映射 + 指纹 + 平台）", async () => {
    const captured: CapturedRequest[] = [];
    const bridge = makeBridge({
      routes: { "/v1/auth/register": { status: 200, body: envelope(PAIR) } },
      captured,
      platform: "win32",
    });
    const view = await bridge.register({ username: "a@b.com", password: "Passw0rd1" });
    assert.deepEqual(view, { username: "a@b.com", expiresInMs: 900_000 });
    assert.equal(bridge.signedIn(), true);
    assert.deepEqual(captured[0]?.body, {
      email: "a@b.com",
      password: "Passw0rd1",
      fingerprint: "fp-12345678",
      platform: "win32",
    });
  });

  it("注销：请求吊销并清空会话（幂等返回吊销数）", async () => {
    const captured: CapturedRequest[] = [];
    const bridge = makeBridge({
      routes: {
        "/v1/auth/login": { status: 200, body: envelope(PAIR) },
        "/v1/auth/logout": { status: 200, body: envelope({ revoked: 1 }) },
      },
      captured,
    });
    await bridge.login({ username: "alice", password: "secret" });
    assert.equal(await bridge.logout(), 1);
    assert.equal(bridge.signedIn(), false);
    assert.equal(await bridge.getToken(), null);
    assert.deepEqual(captured[1]?.body, { refresh_token: "rt-1" });
    assert.equal(await bridge.logout(), 0); // 已登出：不再请求服务端
    assert.equal(captured.length, 2);
  });

  it("注销吊销失败（500）→ unavailable 归因，本地会话仍已清空", async () => {
    const bridge = makeBridge({
      routes: {
        "/v1/auth/login": { status: 200, body: envelope(PAIR) },
        "/v1/auth/logout": { status: 500, body: JSON.stringify({ code: 50001, message: "服务内部错误", detail: null }) },
      },
      captured: [],
    });
    await bridge.login({ username: "alice", password: "secret" });
    await assert.rejects(
      () => bridge.logout(),
      (error: unknown) => error instanceof AuthFailureError && error.reason === "unavailable",
    );
    assert.equal(bridge.signedIn(), false); // 本地会话已清（服务端令牌到期自失效）
  });

  it("登录 401 → credentials 归因，中文可读", async () => {
    const bridge = makeBridge({
      routes: { "/v1/auth/login": { status: 401, body: JSON.stringify({ code: 40103, message: "用户名或密码错误", detail: null }) } },
      captured: [],
    });
    await assert.rejects(
      () => bridge.login({ username: "alice", password: "bad" }),
      (error: unknown) =>
        error instanceof AuthFailureError && error.reason === "credentials" && /账号或密码错误/.test(error.message),
    );
  });

  it("注册 409 → conflict 归因（已注册引导直接登录）", async () => {
    const bridge = makeBridge({
      routes: {
        "/v1/auth/register": {
          status: 409,
          body: JSON.stringify({ code: 40901, message: "资源状态冲突或请求重复", detail: "该邮箱或手机号已注册" }),
        },
      },
      captured: [],
    });
    await assert.rejects(
      () => bridge.register({ username: "a@b.com", password: "Passw0rd1" }),
      (error: unknown) => error instanceof AuthFailureError && error.reason === "conflict",
    );
  });

  it("网络不可达 → network 归因", async () => {
    const bridge = new CloudAuthBridge({
      baseUrl: "http://stub",
      fingerprint: "fp-12345678",
      maxRetries: 0,
      fetchImpl: async () => {
        throw new TypeError("fetch failed");
      },
    });
    await assert.rejects(
      () => bridge.login({ username: "alice", password: "secret" }),
      (error: unknown) =>
        error instanceof AuthFailureError &&
        error.reason === "network" &&
        /无法连接云端服务/.test(error.message),
    );
  });

  it("会话恢复：持久化令牌直接进入登录态，currentView 返回账号与剩余有效期", () => {
    const store = new InMemoryTokenStore();
    store.save({ pair: PAIR, issued_at_ms: BASE_TOKEN_MS, username: "alice" });
    const bridge = new CloudAuthBridge({
      baseUrl: "http://stub",
      fingerprint: "fp-12345678",
      store,
      clock: () => BASE_TOKEN_MS + 60_000,
      maxRetries: 0,
      fetchImpl: async () => {
        throw new Error("恢复路径不应发起网络请求");
      },
    });
    assert.equal(bridge.signedIn(), true);
    assert.deepEqual(bridge.currentView(), { username: "alice", expiresInMs: 900_000 - 60_000 });
  });

  it("登录写入加密存储：账号与令牌可跨实例恢复（模拟重启），密文不含明文", async () => {
    const secrets = new InMemorySecretStore();
    const encryptor = new XorEncryptor("test-key");
    const captured: CapturedRequest[] = [];
    const bridge = new CloudAuthBridge({
      baseUrl: "http://stub",
      fingerprint: "fp-12345678",
      store: createEncryptedTokenStore({ secrets, encryptor }),
      clock: () => BASE_TOKEN_MS,
      maxRetries: 0,
      fetchImpl: stubFetch({ "/v1/auth/login": { status: 200, body: envelope(PAIR) } }, captured),
    });
    await bridge.login({ username: "alice", password: "secret" });
    const ciphertext = secrets.get(SESSION_SCOPE) ?? "";
    assert.ok(ciphertext.length > 0 && !ciphertext.includes("rt-1"), "密文不得包含明文令牌");

    // 模拟重启：同一密文存储新建实例 → 免登录恢复
    const restarted = new CloudAuthBridge({
      baseUrl: "http://stub",
      fingerprint: "fp-12345678",
      store: createEncryptedTokenStore({ secrets, encryptor }),
      clock: () => BASE_TOKEN_MS + 1000,
      maxRetries: 0,
      fetchImpl: async () => {
        throw new Error("恢复路径不应发起网络请求");
      },
    });
    assert.equal(restarted.signedIn(), true);
    assert.equal(restarted.currentView()?.username, "alice");
  });

  it("空账号 → invalid 且不发起请求", async () => {
    const captured: CapturedRequest[] = [];
    const bridge = makeBridge({ routes: {}, captured });
    await assert.rejects(
      () => bridge.login({ username: "   ", password: "secret" }),
      (error: unknown) => error instanceof AuthFailureError && error.reason === "invalid",
    );
    await assert.rejects(
      () => bridge.register({ username: "", password: "Passw0rd1" }),
      (error: unknown) => error instanceof AuthFailureError && error.reason === "invalid",
    );
    assert.equal(captured.length, 0);
  });
});