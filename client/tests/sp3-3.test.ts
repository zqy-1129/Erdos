/**
 * SP3-3 权益快照与离线宽限测试：
 * 真 Ed25519 密钥对验签（对齐 contracts/snapshot.md 规范化字节序）、
 * 防回拨/设备计数器、72h 宽限、欠费冻结、离线账本与联网对账（DF-005）。
 */

import assert from "node:assert/strict";
import { generateKeyPairSync, sign as edSign } from "node:crypto";
import { describe, it } from "node:test";

import { InMemoryOfflineLedger } from "../main/entitlement/offline-ledger.ts";
import {
  EntitlementService,
} from "../main/entitlement/service.ts";
import type {
  EntitlementPayload,
  EntitlementSnapshot,
  OfflineSyncItem,
  OfflineSyncResult,
} from "../main/entitlement/types.ts";
import { EntitlementError } from "../main/entitlement/types.ts";
import { canonical, canonicalBytes, verifySignature } from "../main/entitlement/verify.ts";

const KEY = generateKeyPairSync("ed25519");
const PUBLIC_DER = KEY.publicKey.export({ type: "spki", format: "der" });

const BASE = 1_767_744_000_000; // 2026-01-01T00:00:00Z

function basePayload(over: Partial<EntitlementPayload> = {}): EntitlementPayload {
  return {
    subscribed: false,
    sub_end_at: null,
    purchased_balance: 100,
    monthly_balance: 0,
    frozen: false,
    ...over,
  };
}

function signPayload(payload: object): string {
  return edSign(null, canonicalBytes(payload), KEY.privateKey).toString("hex");
}

function makeSnapshot(
  payload: EntitlementPayload,
  issuedAtMs: number,
  keyVersion = "v1",
): EntitlementSnapshot {
  return {
    payload,
    signature: signPayload(payload),
    key_version: keyVersion,
    issued_at: new Date(issuedAtMs).toISOString(),
  };
}

class Harness {
  now = BASE;
  readonly fetches: EntitlementSnapshot[] = [];
  readonly uploads: OfflineSyncItem[][] = [];
  uploadResult: OfflineSyncResult = { applied: 0, duplicate: 0, insufficient: 0, frozen: false };
  uploadError: Error | null = null;
  readonly service = new EntitlementService({
    fetcher: { fetch: async () => this.fetches.shift()! },
    keyResolver: { resolve: async (v) => (v === "v1" ? PUBLIC_DER : null) },
    uploader: {
      upload: async (items) => {
        this.uploads.push(items);
        if (this.uploadError) throw this.uploadError;
        const result = { ...this.uploadResult, applied: items.length };
        return result;
      },
    },
    clock: () => this.now,
    graceMs: 72 * 3600 * 1000,
  });

  advance(ms: number): void {
    this.now += ms;
  }

  /** 预置一份已签名快照供拉取。 */
  queueSnapshot(payload: EntitlementPayload, step = 0): void {
    this.fetches.push(makeSnapshot(payload, BASE + step * 1000));
  }

  async refresh(payload: EntitlementPayload = basePayload(), step = 0): Promise<void> {
    this.queueSnapshot(payload, step);
    await this.service.refresh();
  }
}

// ---------------------------------------------------------------------------
// 规范化字节序（与服务端 Python json.dumps 严格一致）
// ---------------------------------------------------------------------------
describe("规范化字节序", () => {
  it("与 snapshot.md 示例严格一致", () => {
    const payload = {
      frozen: false,
      monthly_balance: 400,
      purchased_balance: 100,
      sub_end_at: "2026-11-01T00:00:00+00:00",
      subscribed: true,
    };
    assert.equal(
      canonical(payload),
      '{"frozen":false,"monthly_balance":400,"purchased_balance":100,"sub_end_at":"2026-11-01T00:00:00+00:00","subscribed":true}',
    );
    assert.deepEqual(canonicalBytes(payload), Buffer.from(canonical(payload), "utf8"));
  });

  it("非 ASCII 转义为 \\uXXXX（对齐 ensure_ascii=True）", () => {
    assert.equal(canonical({ k: "竞赛" }), '{"k":"\\u7ade\\u8d5b"}');
  });

  it("嵌套对象键排序 + 数组 + null", () => {
    assert.equal(
      canonical({ b: [1, null, "x"], a: { z: true, y: 2 } }),
      '{"a":{"y":2,"z":true},"b":[1,null,"x"]}',
    );
  });

  it("反馈非整数字段拒绝规范化（金额/积分必须整数）", () => {
    assert.throws(() => canonical({ points: 1.5 }));
  });
});

// ---------------------------------------------------------------------------
// 验签与快照应用
// ---------------------------------------------------------------------------
describe("快照验签与防回拨", () => {
  it("合法签名通过；篡改任一字段失败且旧快照保留", async () => {
    const h = new Harness();
    await h.refresh();
    assert.equal(h.service.status(), "ready");

    h.queueSnapshot(basePayload({ purchased_balance: 999 }), 1); // 篡改后重新"签"会通过——因此这里验证篡改：签名对原payload，发送篡改payload
    const tampered = basePayload({ purchased_balance: 999 });
    h.fetches[0] = { ...makeSnapshot(basePayload(), BASE + 1000), payload: tampered };
    await assert.rejects(
      h.service.refresh(),
      (e: unknown) => e instanceof EntitlementError && e.code === "SIGNATURE_INVALID",
    );
    assert.equal(h.service.snapshot()?.payload.purchased_balance, 100); // 旧快照保留
  });

  it("未知 key_version 验签失败", async () => {
    const h = new Harness();
    h.fetches.push(makeSnapshot(basePayload(), BASE, "v9"));
    await assert.rejects(
      h.service.refresh(),
      (e: unknown) => e instanceof EntitlementError && e.code === "SIGNATURE_INVALID",
    );
  });

  it("重放/更旧 issued_at 被拒（设备计数器不变）", async () => {
    const h = new Harness();
    await h.refresh(basePayload(), 5);
    assert.equal(h.service.counter(), 1);
    h.queueSnapshot(basePayload(), 5); // 相同签发时间（重新签名也不行）
    await assert.rejects(
      h.service.refresh(),
      (e: unknown) => e instanceof EntitlementError && e.code === "SNAPSHOT_REPLAY",
    );
    assert.equal(h.service.counter(), 1); // 拒绝应用不增加计数器
  });

  it("verifySignature 对非法 hex / 长度错误返回 false", () => {
    const payloadBytes = canonicalBytes(basePayload());
    assert.equal(verifySignature(payloadBytes, "zz", PUBLIC_DER), false);
    assert.equal(verifySignature(payloadBytes, "abcd", PUBLIC_DER), false);
    assert.equal(verifySignature(payloadBytes, signPayload(basePayload()), PUBLIC_DER), true);
  });
});

// ---------------------------------------------------------------------------
// 72h 宽限与欠费冻结
// ---------------------------------------------------------------------------
describe("72h 宽限与冻结", () => {
  it("超宽限拒绝新阶段，联网刷新恢复", async () => {
    const h = new Harness();
    await h.refresh();
    h.advance(72 * 3600 * 1000 + 1);
    assert.equal(h.service.status(), "grace_expired");
    assert.throws(
      () => h.service.reserve("e1", null, "analysis", 10),
      (e: unknown) => e instanceof EntitlementError && e.code === "GRACE_EXPIRED",
    );
    h.queueSnapshot(basePayload(), 1);
    await h.service.refresh();
    assert.equal(h.service.status(), "ready");
    assert.ok(h.service.reserve("e1", null, "analysis", 10));
  });

  it("宽限内允许新阶段", async () => {
    const h = new Harness();
    await h.refresh();
    h.advance(72 * 3600 * 1000 - 1);
    assert.equal(h.service.status(), "ready");
    assert.ok(h.service.reserve("e1", null, "analysis", 10));
  });

  it("服务端冻结标记：拒绝新阶段（FROZEN）", async () => {
    const h = new Harness();
    await h.refresh(basePayload({ frozen: true }));
    assert.equal(h.service.status(), "frozen");
    assert.throws(
      () => h.service.reserve("e1", null, "analysis", 10),
      (e: unknown) => e instanceof EntitlementError && e.code === "FROZEN",
    );
  });
});

// ---------------------------------------------------------------------------
// 离线许可与账本
// ---------------------------------------------------------------------------
describe("离线许可与流水账本", () => {
  it("余额校验：预扣合计不超本地可用积分；退还恢复额度", async () => {
    const h = new Harness();
    await h.refresh(basePayload({ purchased_balance: 100, monthly_balance: 0 }));
    h.service.reserve("e1", null, "analysis", 60);
    assert.throws(
      () => h.service.reserve("e2", null, "solve", 50),
      (e: unknown) => e instanceof EntitlementError && e.code === "INSUFFICIENT",
    );
    h.service.release("e1");
    assert.ok(h.service.reserve("e2", null, "solve", 50)); // 释放后额度恢复
  });

  it("同 exec_id 幂等：不重复记账", async () => {
    const h = new Harness();
    await h.refresh(basePayload({ purchased_balance: 100 }));
    h.service.reserve("e1", null, "analysis", 30);
    h.service.reserve("e1", null, "analysis", 30);
    assert.ok(h.service.reserve("e2", null, "solve", 70)); // 仅一条 30 分占用
  });

  it("confirm 计扣 / release 不计入待补扣净值", () => {
    const ledger = new InMemoryOfflineLedger();
    ledger.append({ exec_id: "a", task_id: null, stage: "analysis", points: 10 });
    ledger.append({ exec_id: "b", task_id: null, stage: "solve", points: 20 });
    assert.equal(ledger.pendingNet(), 30);
    ledger.markStatus("a", "confirmed");
    assert.equal(ledger.pendingNet(), 30);
    ledger.markStatus("b", "released");
    assert.equal(ledger.pendingNet(), 10);
  });
});

// ---------------------------------------------------------------------------
// 联网对账（DF-005）
// ---------------------------------------------------------------------------
describe("联网对账与快照重建", () => {
  it("批量上报 → 标记 → 快照重建；余额回读为服务端口径", async () => {
    const h = new Harness();
    await h.refresh(basePayload({ purchased_balance: 100 }), 1);
    h.service.reserve("e1", "t1", "analysis", 30);
    h.service.confirm("e1");
    h.service.reserve("e2", null, "solve", 20);
    h.queueSnapshot(basePayload({ purchased_balance: 50 }), 2); // 服务端扣减后的新快照

    const { sync } = await h.service.reconcile();
    assert.ok(sync);
    assert.equal(h.uploads.length, 1);
    assert.deepEqual(
      h.uploads[0]!.map((i) => i.exec_id),
      ["e1", "e2"],
    );
    assert.equal(h.service.snapshot()?.payload.purchased_balance, 50); // 快照重建
    assert.throws(
      () => h.service.reserve("e3", null, "solve", 60),
      (e: unknown) => e instanceof EntitlementError && e.code === "INSUFFICIENT",
    ); // 以服务端余额 50 为口径
  });

  it("上报失败保留 pending，可下轮重试", async () => {
    const h = new Harness();
    await h.refresh(basePayload(), 1);
    h.service.reserve("e1", null, "analysis", 10);

    h.uploadError = new Error("network down");
    await assert.rejects(
      h.service.reconcile(),
      (e: unknown) => e instanceof EntitlementError && e.code === "UPLOAD_FAILED",
    );

    h.uploadError = null;
    h.queueSnapshot(basePayload({ purchased_balance: 90 }), 2); // 补扣后的快照
    const { sync } = await h.service.reconcile();
    assert.equal(sync?.applied, 1);
    assert.equal(h.uploads.length, 2); // 第二次携带同一条 pending
    assert.equal(h.service.snapshot()?.payload.purchased_balance, 90);
    assert.throws(
      () => h.service.reserve("e2", null, "solve", 91),
      (e: unknown) => e instanceof EntitlementError && e.code === "INSUFFICIENT",
    );
  });
});