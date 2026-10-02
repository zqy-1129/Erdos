/**
 * SP3-5 云端集成测试：本地 HTTP 桩服模拟 /v1/auth/jwks、/v1/entitlements/snapshot、
 * /v1/points/offline-sync，验证信封解包、重试/退避、JWKS→SPKI DER 转换、
 * 以及 SP3-3 权益服务接真实云端的 DF-005 全链路（验签→离线许可→批量补扣→快照重建）。
 */

import assert from "node:assert/strict";
import { generateKeyPairSync, sign as edSign, verify as edVerify } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, it } from "node:test";

import { canonicalBytes } from "../main/entitlement/verify.ts";
import type { OfflineSyncItem } from "../main/entitlement/types.ts";
import { EntitlementError } from "../main/entitlement/types.ts";
import { CloudApiError, parseJson, unwrapEnvelope } from "../main/cloud/envelope.ts";
import { createCloudEntitlementService } from "../main/cloud/entitlement-cloud.ts";
import { CloudHttpClient, type FetchLike } from "../main/cloud/http.ts";
import { JwksKeyResolver, okpPublicToSpkiDer } from "../main/cloud/jwks.ts";

const KEY = generateKeyPairSync("ed25519");
const PUBLIC_JWK = KEY.publicKey.export({ format: "jwk" });
const X = PUBLIC_JWK.x!;
const BASE = 1_767_744_000_000; // 2026-01-01T00:00:00Z

function envelope(data: unknown): string {
  return JSON.stringify({ code: 0, message: "ok", detail: null, data });
}

interface StubState {
  balance: number;
  issueStep: number;
  knownExecIds: Set<string>;
  uploadedItems: OfflineSyncItem[];
  jwksHits: number;
  lastAuth: string | null;
}

function signPayload(payload: object): string {
  return edSign(null, canonicalBytes(payload), KEY.privateKey).toString("hex");
}

async function startStub(state: StubState) {
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const url = req.url ?? "";
    res.setHeader("content-type", "application/json");
    state.lastAuth = req.headers["authorization"] ?? null;
    if (req.method === "GET" && url.startsWith("/v1/auth/jwks")) {
      state.jwksHits += 1;
      res.end(
        envelope({
          keys: [{ kty: "OKP", crv: "Ed25519", kid: "v1", use: "sig", alg: "EdDSA", x: X }],
        }),
      );
      return;
    }
    if (req.method === "GET" && url.startsWith("/v1/entitlements/snapshot")) {
      const issuedAt = new Date(BASE + state.issueStep++ * 1000).toISOString();
      const payload = {
        subscribed: false,
        sub_end_at: null,
        purchased_balance: state.balance,
        monthly_balance: 0,
        frozen: false,
        issued_at: issuedAt, // 纳入签名载荷（对齐服务端）
      };
      res.end(
        envelope({
          payload,
          signature: signPayload(payload),
          key_version: "v1",
          issued_at: issuedAt,
        }),
      );
      return;
    }
    if (req.method === "POST" && url.startsWith("/v1/points/offline-sync")) {
      let raw = "";
      req.on("data", (chunk: Buffer) => (raw += chunk.toString()));
      req.on("end", () => {
        const items = (JSON.parse(raw) as { items: OfflineSyncItem[] }).items ?? [];
        let applied = 0;
        for (const item of items) {
          state.uploadedItems.push(item);
          if (!state.knownExecIds.has(item.exec_id)) {
            state.knownExecIds.add(item.exec_id);
            state.balance -= item.points;
            applied += 1;
          }
        }
        res.end(envelope({ applied, duplicate: items.length - applied, insufficient: 0, frozen: false }));
      });
      return;
    }
    res.statusCode = 404;
    res.end(JSON.stringify({ code: 40401, message: "not found", detail: null }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    state,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

/** 可控故障 fetch 包装。 */
function flakyFetch(
  failures: number,
  real: typeof fetch,
): { impl: FetchLike; calls: () => number } {
  let calls = 0;
  return {
    calls: () => calls,
    impl: async (input, init) => {
      calls += 1;
      if (calls <= failures) throw new TypeError("network down");
      return real(input, init);
    },
  };
}

// ---------------------------------------------------------------------------
// 信封与错误
// ---------------------------------------------------------------------------
describe("统一信封解包", () => {
  it("code=0 返回 data；业务错误抛 CloudApiError 且携带 detail", () => {
    const value = unwrapEnvelope<{ a: number }>(
      { code: 0, message: "ok", detail: null, data: { a: 1 } },
      200,
    );
    assert.equal(value.a, 1);
    assert.throws(
      () => unwrapEnvelope({ code: 40901, message: "冲突", detail: "余额不足" }, 409),
      (e: unknown) =>
        e instanceof CloudApiError && e.code === 40901 && e.message.includes("余额不足"),
    );
  });

  it("非信封形状 / 非法 JSON 抛协议错误", () => {
    assert.throws(() => unwrapEnvelope({ foo: 1 }, 200), CloudApiError);
    assert.throws(() => parseJson("not-json", 200), CloudApiError);
  });
});

// ---------------------------------------------------------------------------
// HTTP 客户端：重试与鉴权
// ---------------------------------------------------------------------------
describe("CloudHttpClient 重试与鉴权", () => {
  it("网络错误按退避重试后成功", async () => {
    const stub = await startStub({
      balance: 100, issueStep: 0, knownExecIds: new Set(), uploadedItems: [], jwksHits: 0, lastAuth: null,
    });
    try {
      const flaky = flakyFetch(1, fetch);
      const http = new CloudHttpClient({ baseUrl: stub.baseUrl, fetchImpl: flaky.impl });
      const jwks = await http.get<{ keys: unknown[] }>("/v1/auth/jwks");
      assert.equal(jwks.keys.length, 1);
      assert.equal(flaky.calls(), 2); // 首次失败 + 重试成功
    } finally {
      await stub.close();
    }
  });

  it("网络错误全部失败：重试 maxRetries 次后抛出", async () => {
    const never = flakyFetch(99, fetch);
    const http = new CloudHttpClient({
      baseUrl: "http://127.0.0.1:1",
      fetchImpl: never.impl,
      retryBackoffMs: 1,
      timeoutMs: 2000,
    });
    await assert.rejects(http.get("/v1/auth/jwks"), CloudApiError);
    assert.equal(never.calls(), 3); // 1 + 2 次重试
  });

  it("401 与业务错误不重试", async () => {
    const calls = { count: 0 };
    const http = new CloudHttpClient({
      baseUrl: "http://x",
      fetchImpl: async () => {
        calls.count += 1;
        return { status: 401, ok: false, text: async () => JSON.stringify({ code: 40101, message: "未认证", detail: null }) };
      },
    });
    await assert.rejects(
      http.get("/v1/entitlements/snapshot"),
      (e: unknown) => e instanceof CloudApiError && e.httpStatus === 401,
    );
    assert.equal(calls.count, 1);
  });

  it("令牌提供者注入 Bearer 头", async () => {
    const stub = await startStub({
      balance: 0, issueStep: 0, knownExecIds: new Set(), uploadedItems: [], jwksHits: 0, lastAuth: null,
    });
    try {
      const http = new CloudHttpClient({
        baseUrl: stub.baseUrl,
        getToken: async () => "tok-42",
      });
      await http.get("/v1/auth/jwks");
      assert.equal(stub.state.lastAuth, "Bearer tok-42");
    } finally {
      await stub.close();
    }
  });
});

// ---------------------------------------------------------------------------
// JWKS → SPKI DER
// ---------------------------------------------------------------------------
describe("JWKS 解析", () => {
  it("OKP/Ed25519 x → SPKI DER 可验签；非法输入返回 null", () => {
    const der = okpPublicToSpkiDer(X);
    assert.ok(der);
    const payload = canonicalBytes({ frozen: false });
    const sig = edSign(null, payload, KEY.privateKey).toString("hex");
    assert.equal(
      edVerify(null, payload, { key: der, format: "der", type: "spki" }, Buffer.from(sig, "hex")),
      true,
    );
    assert.equal(okpPublicToSpkiDer("abc"), null); // 非法长度
    assert.equal(okpPublicToSpkiDer("@@@"), null); // 非法 base64url
  });

  it("按 kid 缓存：重复 resolve 只拉取一次", async () => {
    const stub = await startStub({
      balance: 0, issueStep: 0, knownExecIds: new Set(), uploadedItems: [], jwksHits: 0, lastAuth: null,
    });
    try {
      const resolver = new JwksKeyResolver(new CloudHttpClient({ baseUrl: stub.baseUrl }));
      const a = await resolver.resolve("v1");
      const b = await resolver.resolve("v1");
      assert.ok(a && b);
      assert.equal(stub.state.jwksHits, 1);
      assert.equal(stub.state.jwksHits, 1);
      assert.equal(await resolver.resolve("v9"), null); // 未知 kid 触发一次重新拉取后仍为 null
    } finally {
      await stub.close();
    }
  });
});

// ---------------------------------------------------------------------------
// DF-005 全链路（桩服 + 真 Ed25519）
// ---------------------------------------------------------------------------
describe("云端权益服务全链路（DF-005）", () => {
  it("验签→离线许可→批量补扣→快照重建→余额回读", async () => {
    const state: StubState = {
      balance: 100,
      issueStep: 0,
      knownExecIds: new Set(),
      uploadedItems: [],
      jwksHits: 0,
      lastAuth: null,
    };
    const stub = await startStub(state);
    try {
      const service = createCloudEntitlementService({ baseUrl: stub.baseUrl });
      await service.refresh();
      assert.equal(service.status(), "ready");

      service.reserve("e1", "t1", "analysis", 30);
      service.confirm("e1");
      service.reserve("e2", null, "solve", 20);

      const { sync } = await service.reconcile();
      assert.equal(sync?.applied, 2);
      assert.deepEqual(state.uploadedItems.map((i) => i.exec_id), ["e1", "e2"]);
      assert.equal(service.snapshot()?.payload.purchased_balance, 50);

      // 以服务端余额 50 为口径：60 分应拒
      assert.throws(
        () => service.reserve("e3", null, "solve", 60),
        (e: unknown) => e instanceof EntitlementError && e.code === "INSUFFICIENT",
      );
    } finally {
      await stub.close();
    }
  });

  it("服务端冻结快照 → 客户端状态 frozen 且拒绝新阶段", async () => {
    // 自定义桩：快照固定 frozen=true（覆盖默认 startStub 行为）
    const server = createServer((req: IncomingMessage, res: ServerResponse) => {
      res.setHeader("content-type", "application/json");
      const url = req.url ?? "";
      if (url.startsWith("/v1/auth/jwks")) {
        res.end(
          envelope({
            keys: [{ kty: "OKP", crv: "Ed25519", kid: "v1", use: "sig", alg: "EdDSA", x: X }],
          }),
        );
        return;
      }
      const payload = {
        subscribed: false,
        sub_end_at: null,
        purchased_balance: 10,
        monthly_balance: 0,
        frozen: true,
        issued_at: new Date(BASE).toISOString(),
      };
      res.end(
        envelope({
          payload,
          signature: signPayload(payload),
          key_version: "v1",
          issued_at: new Date(BASE).toISOString(),
        }),
      );
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = (server.address() as AddressInfo).port;
    try {
      const service = createCloudEntitlementService({ baseUrl: `http://127.0.0.1:${port}` });
      await service.refresh();
      assert.equal(service.status(), "frozen");
      assert.throws(
        () => service.reserve("e1", null, "analysis", 5),
        (e: unknown) => e instanceof EntitlementError && e.code === "FROZEN",
      );
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});