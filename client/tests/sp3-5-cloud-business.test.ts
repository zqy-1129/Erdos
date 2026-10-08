/**
 * 云端业务通道接线测试（SP3-5 应用层）：权益视图（快照拉取 + JWKS 验签 + 72h 宽限）、
 * 账单（订阅/余额/流水）映射与形状校验、错误路径（401 清会话 / 验签失败 / 响应漂移）。
 * 末尾为真实服务端联调（不可达自动 skip）。
 */

import assert from "node:assert/strict";
import { generateKeyPairSync, sign as edSign } from "node:crypto";
import { describe, it } from "node:test";

import { CloudAuthBridge } from "../main/ipc/cloud-auth.ts";
import {
  CloudBusinessBridge,
  billingOverviewOf,
  entitlementViewOf,
  ledgerRowOf,
  normalizeIso,
  parseBalance,
  parseLedgerPage,
  parseSubscription,
  pickSubscription,
} from "../main/ipc/cloud-business.ts";
import { EntitlementService, InMemoryEntitlementStateStore } from "../main/entitlement/service.ts";
import type { EntitlementStateStore } from "../main/entitlement/service.ts";
import type { EntitlementPayload } from "../main/entitlement/types.ts";
import { canonicalBytes } from "../main/entitlement/verify.ts";
import { newFingerprint } from "../main/device-identity.ts";
import type { FetchLike } from "../main/cloud/http.ts";

// ---------------------------------------------------------------------------
// 测试资产：真 Ed25519 密钥对（对齐 contracts/snapshot.md 规范化字节序）
// ---------------------------------------------------------------------------

const KEY = generateKeyPairSync("ed25519");
const PUBLIC_DER = KEY.publicKey.export({ type: "spki", format: "der" }) as Buffer;
/** JWKS 发布的原始 32 字节公钥（SPKI DER 去掉 RFC 8410 固定前缀）。 */
const PUBLIC_RAW_BASE64URL = PUBLIC_DER.subarray(12).toString("base64url");

const BASE = 1_767_744_000_000; // 2026-01-01T00:00:00Z
const GRACE_MS = 72 * 3600 * 1000;

function envelope(data: unknown): string {
  return JSON.stringify({ code: 0, message: "ok", detail: null, data });
}

/** 服务端形态签名快照（payload 六字段纳入签名）。 */
function signedSnapshot(issuedAtMs: number, over: Partial<EntitlementPayload> = {}): unknown {
  const payload: EntitlementPayload = {
    subscribed: false,
    sub_end_at: null,
    purchased_balance: 100,
    monthly_balance: 0,
    frozen: false,
    issued_at: new Date(issuedAtMs).toISOString(),
    ...over,
  };
  return {
    payload,
    signature: edSign(null, canonicalBytes(payload), KEY.privateKey).toString("hex"),
    key_version: "v1",
    issued_at: payload.issued_at,
  };
}

const JWKS_ROUTE = {
  "/v1/auth/jwks": {
    status: 200,
    body: envelope({
      keys: [{ kty: "OKP", crv: "Ed25519", kid: "v1", use: "sig", alg: "EdDSA", x: PUBLIC_RAW_BASE64URL }],
    }),
  },
};

interface Captured {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

/** 按路径应答的 fetch 桩（优先精确匹配 path+query，其次 pathname；未登记直接抛错暴露意外请求）。 */
function stubFetch(
  routes: Record<string, { status: number; body: string }>,
  captured: Captured[] = [],
): FetchLike {
  return async (input, init) => {
    const url = String(input);
    captured.push({
      url,
      headers: (init.headers as Record<string, string> | undefined) ?? {},
      body: init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {},
    });
    const parsed = new URL(url);
    const route = routes[parsed.pathname + parsed.search] ?? routes[parsed.pathname];
    if (!route) throw new Error(`未预期请求：${url}`);
    return { status: route.status, ok: route.status >= 200 && route.status < 300, text: async () => route.body };
  };
}

/** 单测旁路：直接装配 EntitlementService（不触网）。 */
function makeEntitlementService(
  now: () => number,
  snapshots: unknown[] = [],
  store?: EntitlementStateStore,
): EntitlementService {
  return new EntitlementService({
    fetcher: {
      fetch: async () => snapshots.shift() as never,
    },
    keyResolver: { resolve: async (kid) => (kid === "v1" ? PUBLIC_DER : null) },
    uploader: {
      upload: async () => ({ applied: 0, duplicate: 0, insufficient: 0, frozen: false }),
    },
    store,
    clock: now,
    graceMs: GRACE_MS,
  });
}

function makeBridge(options: {
  routes: Record<string, { status: number; body: string }>;
  captured?: Captured[];
  now?: () => number;
  token?: string | null;
  onUnauthorized?: (status: number) => void;
  entitlementStore?: EntitlementStateStore | null;
}): CloudBusinessBridge {
  return new CloudBusinessBridge({
    baseUrl: "http://stub",
    getToken: async () => options.token ?? null,
    onUnauthorized: options.onUnauthorized,
    entitlementStore: options.entitlementStore,
    maxRetries: 0,
    fetchImpl: stubFetch(options.routes, options.captured),
    clock: options.now,
  });
}

// ---------------------------------------------------------------------------
// 视图映射与形状校验
// ---------------------------------------------------------------------------

describe("视图映射与形状校验", () => {
  it("ISO 归一：+00:00 → Z（渲染层按 Z 形态截断展示）", () => {
    assert.equal(normalizeIso("2026-10-08T12:00:00+00:00"), "2026-10-08T12:00:00Z");
    assert.equal(normalizeIso("2026-10-08T12:00:00Z"), "2026-10-08T12:00:00Z");
  });

  it("订阅解析：null=无该计划订阅；字段非法即抛错", () => {
    assert.equal(parseSubscription(null), null);
    assert.deepEqual(
      parseSubscription({ plan: "yearly", status: "active", end_at: "2026-11-02T00:00:00+00:00" }),
      { plan: "yearly", status: "active", end_at: "2026-11-02T00:00:00+00:00" },
    );
    assert.throws(() => parseSubscription({ plan: 1 }), /订阅响应形状非法/);
    assert.throws(() => parseSubscription({ plan: "monthly", end_at: "2026-11-02T00:00:00Z" }), /订阅响应形状非法/);
  });

  it("多计划择取：active 优先；无 active 取到期更晚者；全空=null", () => {
    const monthly = { plan: "monthly", status: "expired", end_at: "2026-10-01T00:00:00Z" };
    const yearlyActive = { plan: "yearly", status: "active", end_at: "2027-10-01T00:00:00Z" };
    const yearlyExpired = { plan: "yearly", status: "expired", end_at: "2027-10-01T00:00:00Z" };
    assert.equal(pickSubscription([null, null]), null);
    assert.deepEqual(pickSubscription([monthly, null]), monthly);
    assert.deepEqual(pickSubscription([monthly, yearlyActive]), yearlyActive);
    assert.deepEqual(pickSubscription([monthly, yearlyExpired]), yearlyExpired); // 均非 active：取更晚到期
  });

  it("余额解析：双余额数值校验", () => {
    assert.deepEqual(parseBalance({ purchased_balance: 20, monthly_balance: 80 }), {
      purchased_balance: 20,
      monthly_balance: 80,
    });
    assert.throws(() => parseBalance({ purchased_balance: "20" }), /余额响应形状非法/);
    assert.throws(() => parseBalance(null), /余额响应形状非法/);
  });

  it("流水页解析：逐条校验（stage 允许 null），漂移即抛错", () => {
    const page = parseLedgerPage({
      items: [
        { exec_id: "e1", delta: 100, kind: "grant", stage: null, created_at: "2026-10-08T12:00:00+00:00" },
        { exec_id: "e2", delta: -3, kind: "consume", stage: "solving", created_at: "2026-10-08T13:00:00Z" },
      ],
      total: 2,
    });
    assert.equal(page.items.length, 2);
    assert.equal(page.total, 2);
    assert.throws(() => parseLedgerPage({ items: [{ exec_id: "e" }] }), /流水响应形状非法/);
    assert.throws(() => parseLedgerPage({ total: 1 }), /流水响应形状非法/);
    assert.throws(() => parseLedgerPage({ items: [] }), /流水响应形状非法/); // 缺 total 亦视为漂移
  });

  it("账单总览映射：无订阅=免费版；有订阅取 plan/到期日（归一 Z）", () => {
    assert.deepEqual(billingOverviewOf(null, { purchased_balance: 20, monthly_balance: 80 }), {
      planName: "免费版",
      subEndAt: null,
      pointsBalance: 100,
    });
    assert.deepEqual(
      billingOverviewOf(
        { plan: "yearly", status: "active", end_at: "2026-11-02T00:00:00+00:00" },
        { purchased_balance: 0, monthly_balance: 500 },
      ),
      { planName: "yearly", subEndAt: "2026-11-02T00:00:00Z", pointsBalance: 500 },
    );
  });

  it("流水行映射：taskId 置空（服务端不暴露任务号）；stage null → 空串", () => {
    assert.deepEqual(
      ledgerRowOf({ exec_id: "e1", delta: -5, kind: "reserve", stage: null, created_at: "2026-10-08T12:00:00+00:00" }),
      { ts: "2026-10-08T12:00:00Z", action: "reserve", stage: "", points: -5, taskId: "" },
    );
  });

  it("权益视图：无快照=empty 零态；已验证快照=ready + 双余额 + 宽限到期", async () => {
    let now = BASE;
    const empty = makeEntitlementService(() => now);
    assert.deepEqual(entitlementViewOf(empty, now), { status: "empty", balance: 0, graceDeadlineMs: null });

    const service = makeEntitlementService(() => now, [signedSnapshot(BASE, { purchased_balance: 20, monthly_balance: 80 })]);
    await service.refresh();
    now += 3600_000; // 已过 1h：宽限到期 = 最近同步 + 72h
    assert.deepEqual(entitlementViewOf(service, now), {
      status: "ready",
      balance: 100,
      graceDeadlineMs: BASE + GRACE_MS,
    });
  });
});

// ---------------------------------------------------------------------------
// CloudBusinessBridge 云链路（fetch 桩）
// ---------------------------------------------------------------------------

describe("CloudBusinessBridge 云链路（fetch 桩）", () => {
  it("权益：快照拉取 + JWKS 验签 + 视图返回（携带登录态令牌）", async () => {
    const captured: Captured[] = [];
    const now = BASE;
    const bridge = makeBridge({
      routes: {
        ...JWKS_ROUTE,
        "/v1/entitlements/snapshot": { status: 200, body: envelope(signedSnapshot(BASE)) },
      },
      captured,
      now: () => now,
      token: "at-1",
    });
    const view = await bridge.entitlement();
    assert.deepEqual(view, { status: "ready", balance: 100, graceDeadlineMs: BASE + GRACE_MS, stale: false });
    const snapshotRequest = captured.find((r) => r.url.endsWith("/v1/entitlements/snapshot"));
    assert.equal(snapshotRequest?.headers["authorization"], "Bearer at-1");
  });

  it("权益：刷新失败但有本地快照（重启恢复）→ 回退本地视图并标 stale", async () => {
    // 上一进程已落盘快照（同一 store 注入：模拟重启后磁盘状态）；本进程网络不可达
    let now = BASE;
    const store = new InMemoryEntitlementStateStore();
    await makeEntitlementService(
      () => now,
      [signedSnapshot(BASE, { purchased_balance: 20, monthly_balance: 80 })],
      store,
    ).refresh();

    now = BASE + 3600_000; // 重启后离线 1h：宽限应按落盘同步时间延续
    const bridge = makeBridge({ routes: {}, now: () => now, entitlementStore: store });
    assert.deepEqual(await bridge.entitlement(), {
      status: "ready",
      balance: 100,
      graceDeadlineMs: BASE + GRACE_MS,
      stale: true,
    });
  });

  it("权益：刷新失败且无本地快照 → 原样抛错（不回退假态）", async () => {
    const bridge = makeBridge({ routes: {}, now: () => BASE });
    await assert.rejects(() => bridge.entitlement(), /未预期请求/);
  });

  it("权益：401 → 不回退本地快照，触发清会话并归一为「登录已失效」", async () => {
    let now = BASE;
    const store = new InMemoryEntitlementStateStore();
    await makeEntitlementService(() => now, [signedSnapshot(BASE)], store).refresh();
    now = BASE + 1000;

    let unauthorized = 0;
    const bridge = makeBridge({
      routes: {
        "/v1/entitlements/snapshot": {
          status: 401,
          body: JSON.stringify({ code: 40101, message: "未认证", detail: null }),
        },
      },
      now: () => now,
      entitlementStore: store,
      onUnauthorized: () => {
        unauthorized += 1;
      },
    });
    await assert.rejects(() => bridge.entitlement(), /登录已失效/);
    assert.equal(unauthorized, 1);
  });

  it("权益：快照 200 但 JWKS 401 → 同样清会话并归一为「登录已失效」（不回退本地快照）", async () => {
    let now = BASE;
    const store = new InMemoryEntitlementStateStore();
    await makeEntitlementService(() => now, [signedSnapshot(BASE)], store).refresh();
    now = BASE + 1000;

    let unauthorized = 0;
    const bridge = makeBridge({
      routes: {
        "/v1/auth/jwks": {
          status: 401,
          body: JSON.stringify({ code: 40101, message: "未认证", detail: null }),
        },
        "/v1/entitlements/snapshot": { status: 200, body: envelope(signedSnapshot(BASE + 1000)) },
      },
      now: () => now,
      entitlementStore: store,
      onUnauthorized: () => {
        unauthorized += 1;
      },
    });
    await assert.rejects(() => bridge.entitlement(), /登录已失效/);
    assert.equal(unauthorized, 1);
  });

  it("resetEntitlement（会话级缓存）：清空后刷新失败不再回退本地快照", async () => {
    const now = BASE;
    const store = new InMemoryEntitlementStateStore();
    await makeEntitlementService(() => now, [signedSnapshot(BASE)], store).refresh();

    const bridge = makeBridge({ routes: {}, now: () => now, entitlementStore: store });
    bridge.resetEntitlement();
    await assert.rejects(
      () => bridge.entitlement(),
      /未预期请求/,
      "登录/登出清空本地快照后，无本地状态可回退（防换账号离线回退泄漏）",
    );
  });

  it("权益：快照被篡改（验签失败）→ 原样抛错（无本地快照时不透出旧值）", async () => {
    const tampered = signedSnapshot(BASE) as { payload: EntitlementPayload; signature: string };
    tampered.payload.purchased_balance = 9999; // 改载荷不重签 → 验签必失败
    const bridge = makeBridge({
      routes: {
        ...JWKS_ROUTE,
        "/v1/entitlements/snapshot": { status: 200, body: envelope(tampered) },
      },
      now: () => BASE,
    });
    await assert.rejects(() => bridge.entitlement(), /快照验签失败/);
  });

  it("账单总览：免费版（无订阅）+ 双余额合计", async () => {
    const bridge = makeBridge({
      routes: {
        "/v1/billing/subscription": { status: 200, body: envelope(null) },
        "/v1/points/balance": {
          status: 200,
          body: envelope({ user_id: "u1", purchased_balance: 20, monthly_balance: 80, frozen: false }),
        },
      },
    });
    assert.deepEqual(await bridge.billingOverview(), {
      planName: "免费版",
      subEndAt: null,
      pointsBalance: 100,
    });
  });

  it("账单总览：按 plan 维度双查并择取（yearly 命中，monthly 为空）", async () => {
    const captured: Captured[] = [];
    const bridge = makeBridge({
      routes: {
        "/v1/billing/subscription?plan=monthly": { status: 200, body: envelope(null) },
        "/v1/billing/subscription?plan=yearly": {
          status: 200,
          body: envelope({ plan: "yearly", status: "active", end_at: "2027-10-01T00:00:00+00:00" }),
        },
        "/v1/points/balance": {
          status: 200,
          body: envelope({ user_id: "u1", purchased_balance: 0, monthly_balance: 500, frozen: false }),
        },
      },
      captured,
    });
    const overview = await bridge.billingOverview();
    assert.equal(overview.planName, "yearly");
    assert.equal(overview.subEndAt, "2027-10-01T00:00:00Z");
    assert.equal(overview.pointsBalance, 500);
    assert.ok(
      captured.some((r) => r.url.endsWith("/v1/billing/subscription?plan=monthly")) &&
        captured.some((r) => r.url.endsWith("/v1/billing/subscription?plan=yearly")),
      "订阅需按契约的 monthly/yearly 两个维度查询",
    );
  });

  it("账单总览：401 → 触发清会话回调并归一为「登录已失效」提示", async () => {
    let unauthorized = 0;
    const bridge = makeBridge({
      routes: {
        "/v1/billing/subscription?plan=monthly": {
          status: 401,
          body: JSON.stringify({ code: 40101, message: "未认证", detail: null }),
        },
        "/v1/billing/subscription?plan=yearly": { status: 200, body: envelope(null) },
        "/v1/points/balance": { status: 200, body: envelope({ purchased_balance: 0, monthly_balance: 0 }) },
      },
      onUnauthorized: () => {
        unauthorized += 1;
      },
    });
    await assert.rejects(() => bridge.billingOverview(), /登录已失效/);
    assert.equal(unauthorized, 1); // 清会话回调幂等；仅被 401 的那条请求触发
  });

  it("账单流水：行映射（含 +00:00 归一与 stage 空值）", async () => {
    const bridge = makeBridge({
      routes: {
        "/v1/points/ledger": {
          status: 200,
          body: envelope({
            items: [
              { exec_id: "register:x", delta: 100, kind: "grant", stage: null, created_at: "2026-10-08T12:00:00+00:00" },
            ],
            total: 1,
          }),
        },
      },
    });
    assert.deepEqual(await bridge.billingLedger(), [
      { ts: "2026-10-08T12:00:00Z", action: "grant", stage: "", points: 100, taskId: "" },
    ]);
  });

  it("响应漂移（余额字段类型错误）→ 抛形状非法（不静默显示错误数据）", async () => {
    const bridge = makeBridge({
      routes: {
        "/v1/billing/subscription": { status: 200, body: envelope(null) },
        "/v1/points/balance": { status: 200, body: envelope({ purchased_balance: "20", monthly_balance: 80 }) },
      },
    });
    await assert.rejects(() => bridge.billingOverview(), /余额响应形状非法/);
  });
});

// ---------------------------------------------------------------------------
// 真实服务端联调（不可达自动 skip）
// ---------------------------------------------------------------------------

const BASE_URL = (process.env.ERDOS_API_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

async function serverReachable(): Promise<boolean> {
  try {
    const response = await fetch(`${BASE_URL}/v1/health`, { signal: AbortSignal.timeout(1500) });
    return response.ok;
  } catch {
    return false;
  }
}

describe("云端正版业务通道联调", () => {
  it("注册赠分 → 权益快照（真 JWKS 验签）/ 账单 / 流水 全链路", async (t) => {
    if (!(await serverReachable())) {
      t.skip(`服务端不可达（${BASE_URL}），跳过联调`);
      return;
    }
    const email = `biz-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
    const auth = new CloudAuthBridge({
      baseUrl: BASE_URL,
      fingerprint: newFingerprint(),
      platform: "test",
      timeoutMs: 5000,
    });
    await auth.register({ username: email, password: "Biz0Pass" });
    const business = new CloudBusinessBridge({
      baseUrl: BASE_URL,
      getToken: () => auth.getToken(),
      timeoutMs: 5000,
    });

    // 权益快照：真服务端签名 + 客户端 JWKS 验签 → 注册赠分 100（settings 默认）
    const view = await business.entitlement();
    assert.equal(view.status, "ready");
    assert.equal(view.balance, 100);
    assert.ok(view.graceDeadlineMs !== null && view.graceDeadlineMs > Date.now(), "宽限到期应晚于当前时刻");

    // 账单总览：未订阅 → 免费版；余额与快照一致
    const overview = await business.billingOverview();
    assert.equal(overview.planName, "免费版");
    assert.equal(overview.subEndAt, null);
    assert.equal(overview.pointsBalance, 100);

    // 流水：注册赠分应有一条 +100 的 grant
    const rows = await business.billingLedger();
    assert.ok(
      rows.some((row) => row.action === "grant" && row.points === 100),
      `流水中应有注册赠分：${JSON.stringify(rows)}`,
    );
  });
});