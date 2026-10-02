/**
 * SP3-5 鉴权客户端测试：登录/刷新/注销信封解包 + SessionTokenProvider
 * 过期自动轮换、并发轮换去重、401 会话终止、存储恢复、注销吊销。
 * 本地 HTTP 桩服模拟 /v1/auth/login|refresh|logout（换代防护语义）。
 */

import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, it } from "node:test";

import {
  AuthClient,
  SessionTokenProvider,
  type StoredSession,
  type TokenPair,
  type TokenStore,
} from "../main/cloud/auth-client.ts";
import { CloudApiError } from "../main/cloud/envelope.ts";
import { CloudHttpClient } from "../main/cloud/http.ts";

function envelope(data: unknown): string {
  return JSON.stringify({ code: 0, message: "ok", detail: null, data });
}

interface AuthStubState {
  loginHits: number;
  refreshHits: number;
  logoutHits: number;
  /** 当前有效刷新令牌（换代防护：旧令牌即失效）。 */
  current: string;
  /** refresh 强制失败（模拟令牌失效）。 */
  failRefresh: boolean;
  /** 令牌序号。 */
  seq: number;
}

function newState(): AuthStubState {
  return { loginHits: 0, refreshHits: 0, logoutHits: 0, current: "", failRefresh: false, seq: 0 };
}

function pairOf(state: AuthStubState): TokenPair {
  const seq = ++state.seq;
  state.current = `rt-${seq}`;
  return {
    access_token: `at-${seq}`,
    refresh_token: `rt-${seq}`,
    token_type: "Bearer",
    expires_in: 300,
    refresh_expires_in: 86_400,
  };
}

async function startStub(state: AuthStubState) {
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const url = req.url ?? "";
    res.setHeader("content-type", "application/json");
    let raw = "";
    req.on("data", (chunk: Buffer) => (raw += chunk.toString()));
    req.on("end", () => {
      const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      if (url === "/v1/auth/login" && req.method === "POST") {
        state.loginHits += 1;
        if (body["password"] === "bad") {
          res.statusCode = 401;
          res.end(JSON.stringify({ code: 40102, message: "账号或密码错误", detail: null }));
          return;
        }
        res.end(envelope(pairOf(state)));
        return;
      }
      if (url === "/v1/auth/refresh" && req.method === "POST") {
        state.refreshHits += 1;
        if (state.failRefresh || body["refresh_token"] !== state.current) {
          res.statusCode = 401;
          res.end(JSON.stringify({ code: 40101, message: "刷新令牌无效或已吊销", detail: null }));
          return;
        }
        res.end(envelope(pairOf(state)));
        return;
      }
      if (url === "/v1/auth/logout" && req.method === "POST") {
        state.logoutHits += 1;
        const revoked = body["refresh_token"] === state.current ? 1 : 0;
        if (revoked === 1) state.current = "";
        res.end(envelope({ revoked }));
        return;
      }
      res.statusCode = 404;
      res.end(JSON.stringify({ code: 40401, message: "not found", detail: null }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

/** 可播种/可审计的令牌存储。 */
class CapturingStore implements TokenStore {
  private current: StoredSession | null;
  readonly saved: (StoredSession | null)[] = [];

  constructor(seed: StoredSession | null = null) {
    this.current = seed;
  }
  save(session: StoredSession | null): void {
    this.saved.push(session);
    this.current = session;
  }
  load(): StoredSession | null {
    return this.current;
  }
}

function makeAuth(baseUrl: string): AuthClient {
  return new AuthClient(new CloudHttpClient({ baseUrl, maxRetries: 0 }));
}

describe("AuthClient 登录/刷新/注销", () => {
  it("login 解包令牌对；密码错误抛业务错误码", async () => {
    const { baseUrl, close } = await startStub(newState());
    try {
      const client = makeAuth(baseUrl);
      const pair = await client.login("alice", "secret");
      assert.equal(pair.access_token, "at-1");
      assert.equal(pair.refresh_token, "rt-1");
      assert.equal(pair.token_type, "Bearer");
      await assert.rejects(
        () => client.login("alice", "bad"),
        (error: unknown): boolean =>
          error instanceof CloudApiError && error.code === null && error.httpStatus === 401,
      );
    } finally {
      await close();
    }
  });

  it("refresh 换代：新令牌有效、旧令牌 401；logout 幂等返回吊销数", async () => {
    const { baseUrl, close } = await startStub(newState());
    try {
      const client = makeAuth(baseUrl);
      const first = await client.login("alice", "secret");
      const second = await client.refresh(first.refresh_token);
      assert.equal(second.access_token, "at-2");
      await assert.rejects(
        () => client.refresh(first.refresh_token), // 旧令牌已被换代吊销
        (error: unknown): boolean => error instanceof CloudApiError && error.httpStatus === 401,
      );
      assert.equal(await client.logout(second.refresh_token), 1);
      assert.equal(await client.logout(second.refresh_token), 0); // 幂等
    } finally {
      await close();
    }
  });
});

describe("SessionTokenProvider 登录态", () => {
  it("未过期直接返回令牌，不触发刷新", async () => {
    const { baseUrl, close } = await startStub(newState());
    try {
      let now = 1_000_000;
      const provider = new SessionTokenProvider({
        auth: makeAuth(baseUrl),
        clock: () => now,
      });
      await provider.login("alice", "secret");
      assert.ok(provider.signedIn());
      assert.equal(await provider.getToken(), "at-1");
      assert.equal(await provider.getToken(), "at-1");
      // 推进到不超 skew 的时间点（300s TTL - 30s skew 内）
      now += 200_000;
      assert.equal(await provider.getToken(), "at-1");
    } finally {
      await close();
    }
  });

  it("过期自动轮换且并发 getToken 共享单次刷新", async () => {
    const state = newState();
    const { baseUrl, close } = await startStub(state);
    try {
      let now = 1_000_000;
      const provider = new SessionTokenProvider({
        auth: makeAuth(baseUrl),
        clock: () => now,
      });
      await provider.login("alice", "secret");
      now += 300_001 + 30_000; // 超过 TTL + skew
      const [a, b] = await Promise.all([provider.getToken(), provider.getToken()]);
      assert.equal(a, "at-2");
      assert.equal(b, "at-2");
      assert.equal(state.refreshHits, 1); // 并发去重：仅一次刷新
    } finally {
      await close();
    }
  });

  it("刷新令牌失效（401）→ 清空会话回退匿名", async () => {
    const state = newState();
    const { baseUrl, close } = await startStub(state);
    try {
      let now = 1_000_000;
      const store = new CapturingStore();
      const provider = new SessionTokenProvider({
        auth: makeAuth(baseUrl),
        store,
        clock: () => now,
      });
      await provider.login("alice", "secret");
      now += 500_000;
      state.failRefresh = true;
      assert.equal(await provider.getToken(), null);
      assert.equal(provider.signedIn(), false);
      assert.equal(store.load(), null);
    } finally {
      await close();
    }
  });

  it("从存储恢复会话并按剩余有效期轮换", async () => {
    const state = newState();
    state.current = "rt-1";
    state.seq = 1; // 下一令牌 at-2/rt-2
    const { baseUrl, close } = await startStub(state);
    try {
      const seed: StoredSession = {
        pair: {
          access_token: "at-1",
          refresh_token: "rt-1",
          token_type: "Bearer",
          expires_in: 300,
          refresh_expires_in: 86_400,
        },
        issued_at_ms: 1_000_000,
      };
      const store = new CapturingStore(seed);
      const provider = new SessionTokenProvider({
        auth: makeAuth(baseUrl),
        store,
        clock: () => 2_000_000, // 已远超有效期
      });
      assert.ok(provider.signedIn()); // 恢复即登录态
      assert.equal(await provider.getToken(), "at-2");
      const restored = store.load();
      assert.ok(restored);
      assert.equal(restored.pair.refresh_token, "rt-2");
    } finally {
      await close();
    }
  });

  it("logout 吊销服务端令牌并清空本地会话", async () => {
    const state = newState();
    const { baseUrl, close } = await startStub(state);
    try {
      const provider = new SessionTokenProvider({
        auth: makeAuth(baseUrl),
        clock: () => Date.now(),
      });
      await provider.login("alice", "secret");
      assert.equal(await provider.logout(), 1);
      assert.equal(state.logoutHits, 1);
      assert.equal(provider.signedIn(), false);
      assert.equal(await provider.getToken(), null);
      assert.equal(await provider.logout(), 0); // 已清空：不再请求服务端
    } finally {
      await close();
    }
  });
});