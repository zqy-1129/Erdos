/**
 * 离线流水账本安全落盘测试（SP3-4 第三批）：
 * 只存密文/读写往返/跨实例持久化/写穿/清盘语义/形状校验与 fail-safe、
 * 跨重启记账延续（待补扣门禁 + 对账标记 + 终态语义）、服务级跨重启余额门禁。
 */
import assert from "node:assert/strict";
import { generateKeyPairSync, sign as edSign } from "node:crypto";
import { describe, it } from "node:test";

import {
  EntitlementService,
  type OfflineSyncUploader,
} from "../main/entitlement/service.ts";
import {
  InMemoryOfflineLedger,
  type OfflineLedger,
} from "../main/entitlement/offline-ledger.ts";
import {
  OFFLINE_LEDGER_SCOPE,
  createEncryptedOfflineLedger,
  parseStoredLedger,
} from "../main/entitlement/ledger-store.ts";
import type {
  EntitlementPayload,
  EntitlementSnapshot,
  OfflineLedgerEntry,
  OfflineSyncItem,
} from "../main/entitlement/types.ts";
import { EntitlementError } from "../main/entitlement/types.ts";
import { canonicalBytes } from "../main/entitlement/verify.ts";
import { InMemorySecretStore, XorEncryptor, type SecretStore } from "../main/key-vault.ts";
import { createCloudEntitlementService } from "../main/cloud/entitlement-cloud.ts";
import { AuthFailureError, CloudAuthBridge } from "../main/ipc/cloud-auth.ts";
import { CloudBusinessBridge } from "../main/ipc/cloud-business.ts";
import { newFingerprint } from "../main/device-identity.ts";

const KEY = generateKeyPairSync("ed25519");
const PUBLIC_DER = KEY.publicKey.export({ type: "spki", format: "der" });
const BASE = 1_767_744_000_000; // 2026-01-01T00:00:00Z
const GRACE_MS = 72 * 3600 * 1000;

function makeSnapshot(
  issuedAtMs: number,
  over: Partial<EntitlementPayload> = {},
): EntitlementSnapshot {
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

/** 加密账本 + 固定时钟（创建时间一致，排序断言依赖插入序稳定）。 */
function makeLedger(secrets: InMemorySecretStore): OfflineLedger {
  return createEncryptedOfflineLedger({
    secrets,
    encryptor: new XorEncryptor("test-key"),
    clock: () => new Date(BASE),
  });
}

/** 注入账本的服务装配（fetcher 队列；不触网；state store 按实例隔离）。 */
function makeService(options: {
  ledger: OfflineLedger;
  snapshots: EntitlementSnapshot[];
  now: () => number;
  uploader?: OfflineSyncUploader;
}): EntitlementService {
  return new EntitlementService({
    fetcher: { fetch: async () => options.snapshots.shift() as EntitlementSnapshot },
    keyResolver: { resolve: async (kid) => (kid === "v1" ? PUBLIC_DER : null) },
    uploader:
      options.uploader ??
      ({ upload: async (items) => ({ applied: items.length, duplicate: 0, insufficient: 0, frozen: false }) } as OfflineSyncUploader),
    ledger: options.ledger,
    clock: options.now,
    graceMs: GRACE_MS,
  });
}

function sampleEntry(over: Partial<OfflineLedgerEntry> = {}): OfflineLedgerEntry {
  return {
    exec_id: "exec-1",
    task_id: "t-1",
    stage: "analysis",
    points: 30,
    status: "reserved",
    created_at: "2026-01-01T00:00:00.000Z",
    uploaded: false,
    ...over,
  };
}

// ---------------------------------------------------------------------------
// 落盘载荷形状校验
// ---------------------------------------------------------------------------

describe("parseStoredLedger 形状校验", () => {
  it("合法条目数组原样通过（含 released/uploaded 与 null 任务号）", () => {
    const entries = [
      sampleEntry(),
      sampleEntry({ exec_id: "exec-2", task_id: null, status: "released" }),
      sampleEntry({ exec_id: "exec-3", status: "confirmed", uploaded: true }),
    ];
    assert.deepEqual(parseStoredLedger(entries), entries);
  });

  it("非数组 / 字段漂移 / 状态枚举外 → null", () => {
    assert.equal(parseStoredLedger({ entries: [sampleEntry()] }), null);
    assert.equal(parseStoredLedger(null), null);
    assert.equal(parseStoredLedger([sampleEntry({ points: "30" as unknown as number })]), null);
    assert.equal(parseStoredLedger([sampleEntry({ points: 1.5 })]), null); // 积分必须为整数
    assert.equal(parseStoredLedger([sampleEntry({ created_at: "not-a-date" })]), null); // 时间必须可解析
    assert.equal(parseStoredLedger([sampleEntry({ status: "pending" as OfflineLedgerEntry["status"] })]), null);
    assert.equal(parseStoredLedger([sampleEntry({ uploaded: 1 as unknown as boolean })]), null);
    assert.equal(parseStoredLedger([sampleEntry({ task_id: 7 as unknown as string })]), null);
  });

  it("exec_id 重复 → null（恢复不得静默丢条目）", () => {
    assert.equal(parseStoredLedger([sampleEntry(), sampleEntry()]), null);
  });
});

// ---------------------------------------------------------------------------
// 加密落盘与跨重启延续
// ---------------------------------------------------------------------------

describe("createEncryptedOfflineLedger 落盘与跨重启", () => {
  it("只存密文；重启恢复全形态条目，待补扣净值与顺序一致", () => {
    const secrets = new InMemorySecretStore();
    const encryptor = new XorEncryptor("test-key");
    const ledger = makeLedger(secrets);

    // 全形态：reserved / confirmed / released（退还）/ uploaded（已对账）
    ledger.append({ exec_id: "exec-1", task_id: "t-1", stage: "analysis", points: 30 });
    ledger.append({ exec_id: "exec-2", task_id: null, stage: "modeling", points: 20 });
    ledger.markStatus("exec-2", "confirmed");
    ledger.append({ exec_id: "exec-3", task_id: null, stage: "solving", points: 15 });
    ledger.markStatus("exec-3", "released");
    ledger.append({ exec_id: "exec-4", task_id: null, stage: "writing", points: 5 });
    ledger.markUploaded(["exec-4"]);

    // 只存密文：原始存储值不含明文片段，且可解密还原
    const raw = secrets.get(OFFLINE_LEDGER_SCOPE);
    assert.ok(raw !== null && raw !== "", "落盘值应为密文");
    assert.ok(!raw.includes("exec-1"), "密文不得含明文 exec_id");
    assert.ok(!raw.includes("reserved"), "密文不得含明文状态");
    assert.ok(encryptor.decrypt(raw).includes("exec-1"), "密文可解密还原（XorEncryptor 往返）");

    // 重启：同一存储重建账本
    const restarted = makeLedger(secrets);
    assert.equal(restarted.get("exec-1")?.status, "reserved");
    assert.equal(restarted.get("exec-2")?.status, "confirmed");
    assert.equal(restarted.get("exec-3")?.status, "released");
    assert.equal(restarted.get("exec-4")?.uploaded, true);
    assert.equal(restarted.pendingNet(), 50); // 30 + 20（released/uploaded 不计）
    assert.deepEqual(
      restarted.pendingItems().map((entry) => entry.exec_id),
      ["exec-1", "exec-2"],
    );
  });

  it("幂等跨重启：同 exec_id 返回既有条目，字段不被覆盖、净值不变", () => {
    const secrets = new InMemorySecretStore();
    const ledger = makeLedger(secrets);
    ledger.append({ exec_id: "exec-1", task_id: "t-1", stage: "analysis", points: 30 });

    const restarted = makeLedger(secrets);
    const again = restarted.append({ exec_id: "exec-1", task_id: null, stage: "solving", points: 99 });
    assert.equal(again.points, 30);
    assert.equal(again.task_id, "t-1");
    assert.equal(restarted.pendingNet(), 30);
  });

  it("状态迁移写穿：confirmed 跨重启延续；released 终态不可再迁；已对账不可退还", () => {
    const secrets = new InMemorySecretStore();
    const ledger = makeLedger(secrets);
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });
    ledger.markStatus("exec-1", "confirmed");
    ledger.append({ exec_id: "exec-2", task_id: null, stage: "analysis", points: 20 });
    ledger.markStatus("exec-2", "released");
    ledger.append({ exec_id: "exec-3", task_id: null, stage: "analysis", points: 5 });
    ledger.markUploaded(["exec-3"]);

    // 终态与已对账守卫（内存态）
    assert.equal(ledger.markStatus("exec-2", "confirmed")?.status, "released");
    assert.equal(ledger.markStatus("exec-3", "released")?.status, "reserved");

    const restarted = makeLedger(secrets);
    assert.equal(restarted.get("exec-1")?.status, "confirmed");
    assert.equal(restarted.get("exec-2")?.status, "released");
    assert.equal(restarted.get("exec-3")?.uploaded, true);
    assert.equal(restarted.get("exec-3")?.status, "reserved"); // 已对账的消耗不可退还
    assert.equal(restarted.pendingNet(), 30); // confirmed 计、released 不计、uploaded 不计
  });

  it("对账标记写穿：markUploaded 跨重启后待补扣归零", () => {
    const secrets = new InMemorySecretStore();
    const ledger = makeLedger(secrets);
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });
    ledger.markUploaded(["exec-1"]);

    const restarted = makeLedger(secrets);
    assert.equal(restarted.get("exec-1")?.uploaded, true);
    assert.equal(restarted.pendingNet(), 0);
  });

  it("clear 清盘：存储条目删除（空密文=已删除语义）+ 重启空账本", () => {
    const secrets = new InMemorySecretStore();
    const ledger = makeLedger(secrets);
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });
    ledger.clear();

    assert.equal(secrets.get(OFFLINE_LEDGER_SCOPE), null); // InMemorySecretStore：空密文即删除
    const restarted = makeLedger(secrets);
    assert.equal(restarted.get("exec-1"), null);
    assert.equal(restarted.pendingNet(), 0);
  });

  it("clear 对「密文损坏残留」也清盘（显式清盘，不留坏密文）", () => {
    const secrets = new InMemorySecretStore();
    secrets.set(OFFLINE_LEDGER_SCOPE, "broken-ciphertext"); // 损坏 → 水合为空账本
    makeLedger(secrets).clear();

    assert.equal(secrets.get(OFFLINE_LEDGER_SCOPE), null);
  });

  it("fail-safe：密文损坏 / 载荷形状漂移 → 空账本（不抛错、不半信半疑）", () => {
    const secrets = new InMemorySecretStore();
    const encryptor = new XorEncryptor("test-key");
    const ledger = makeLedger(secrets);
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });

    // 密文损坏（密钥轮换/篡改）
    secrets.set(OFFLINE_LEDGER_SCOPE, "!!!not-a-valid-ciphertext!!!");
    assert.equal(makeLedger(secrets).pendingNet(), 0);

    // 可解密但形状漂移（旧版本/异物写入）
    secrets.set(OFFLINE_LEDGER_SCOPE, encryptor.encrypt(JSON.stringify({ entries: [] })));
    assert.equal(makeLedger(secrets).pendingNet(), 0);
  });

  it("换密钥重建（密钥轮换/异机）→ 空账本（fail-safe）", () => {
    const secrets = new InMemorySecretStore();
    makeLedger(secrets).append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });

    const rotated = createEncryptedOfflineLedger({
      secrets,
      encryptor: new XorEncryptor("rotated-key"),
      clock: () => new Date(BASE),
    });
    assert.equal(rotated.pendingNet(), 0);
  });

  it("无变化调用零写盘（写计数 spy）：幂等/同态迁移/未知 exec_id/重复上报；真实变更必落盘", () => {
    const base = new InMemorySecretStore();
    let writes = 0;
    const store: SecretStore = {
      get: (scope) => base.get(scope),
      set: (scope, value) => {
        writes += 1;
        base.set(scope, value);
      },
      has: (scope) => base.has(scope),
    };
    const ledger = createEncryptedOfflineLedger({
      secrets: store,
      encryptor: new XorEncryptor("test-key"),
      clock: () => new Date(BASE),
    });
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });
    ledger.markStatus("exec-1", "confirmed");
    assert.equal(writes, 2, "追加与确认各落盘一次");

    // 无变化调用：不产生写放大
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "solving", points: 99 }); // 幂等命中
    ledger.markStatus("exec-1", "confirmed"); // 同态迁移
    assert.equal(ledger.markStatus("nope", "confirmed"), null); // 未知 exec_id
    ledger.markUploaded([]);
    ledger.markUploaded(["nope"]);
    assert.equal(writes, 2, "无变化调用不得写盘");

    // 真实变更：必落盘；重复上报不写放大
    ledger.markUploaded(["exec-1"]);
    assert.equal(writes, 3, "首次对账标记应落盘");
    ledger.markUploaded(["exec-1"]);
    assert.equal(writes, 3, "重复上报不得写盘");
  });

  it("scope 注入：自定义命名空间落盘，不触碰默认 scope", () => {
    const secrets = new InMemorySecretStore();
    const scoped = createEncryptedOfflineLedger({
      secrets,
      encryptor: new XorEncryptor("test-key"),
      scope: "test.ledger",
      clock: () => new Date(BASE),
    });
    scoped.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });

    assert.ok(secrets.get("test.ledger"));
    assert.equal(secrets.get(OFFLINE_LEDGER_SCOPE), null);
  });

  it("落盘失败回滚：变更不生效且抛错（落盘成功才算记账）", () => {
    const base = new InMemorySecretStore();
    let failing = false;
    const store: SecretStore = {
      get: (scope) => base.get(scope),
      set: (scope, value) => {
        if (failing) throw new Error("disk full");
        base.set(scope, value);
      },
      has: (scope) => base.has(scope),
    };
    const encryptor = new XorEncryptor("test-key");
    const ledger = createEncryptedOfflineLedger({ secrets: store, encryptor, clock: () => new Date(BASE) });
    ledger.append({ exec_id: "exec-1", task_id: null, stage: "analysis", points: 30 });

    failing = true;
    assert.throws(
      () => ledger.append({ exec_id: "exec-2", task_id: null, stage: "solving", points: 20 }),
      /disk full/,
    );
    assert.equal(ledger.get("exec-2"), null, "落盘失败：内存回滚");
    assert.equal(ledger.pendingNet(), 30);

    // 清盘落盘失败：同样回滚（显式清盘不留半态）
    assert.throws(() => ledger.clear(), /disk full/, "清盘落盘失败应抛错");
    assert.equal(ledger.pendingNet(), 30, "清盘失败：内存回滚");

    failing = false;
    assert.equal(
      createEncryptedOfflineLedger({ secrets: store, encryptor, clock: () => new Date(BASE) }).pendingNet(),
      30,
      "存储侧与回滚后内存一致",
    );
  });
});

// ---------------------------------------------------------------------------
// 内存实现水合（加密账本内部复用；独立断言防语义漂移）
// ---------------------------------------------------------------------------

describe("InMemoryOfflineLedger 水合与全量导出", () => {
  it("水合恢复全部条目（含终态）；allEntries 返回防御性副本", () => {
    const initial = [sampleEntry(), sampleEntry({ exec_id: "exec-2", status: "released" })];
    const ledger = new InMemoryOfflineLedger(() => new Date(BASE), initial);

    assert.equal(ledger.pendingNet(), 30); // released 不计
    const snapshot = ledger.allEntries();
    assert.equal(snapshot.length, 2);
    snapshot[0]!.points = 999; // 改副本
    assert.equal(ledger.get("exec-1")?.points, 30, "allEntries 副本变更不得影响内部状态");
  });
});

// ---------------------------------------------------------------------------
// 服务级跨重启：余额门禁消费恢复后的待补扣
// ---------------------------------------------------------------------------

describe("离线记账跨重启（服务级）", () => {
  it("重启后待补扣参与余额门禁；对账标记再重启归零；released 不占额度", async () => {
    const secrets = new InMemorySecretStore();
    const now = () => BASE + 60_000;
    const snapshots = [
      makeSnapshot(BASE),
      makeSnapshot(BASE + 1_000),
      makeSnapshot(BASE + 2_000),
      makeSnapshot(BASE + 3_000),
    ];

    // 会话 A：快照 → 记账（e1 确认 / e2 待确认 / e5 退还）
    const ledgerA = makeLedger(secrets);
    const serviceA = makeService({ ledger: ledgerA, snapshots, now });
    await serviceA.refresh();
    serviceA.reserve("e1", "t-1", "analysis", 30);
    serviceA.confirm("e1");
    serviceA.reserve("e2", null, "modeling", 20);
    serviceA.reserve("e5", null, "solving", 40);
    serviceA.release("e5");

    // 重启：待补扣（30 + 20）跨实例延续，released（e5）不占额度
    const ledgerB = makeLedger(secrets);
    assert.equal(ledgerB.pendingNet(), 50);
    assert.equal(ledgerB.get("e1")?.status, "confirmed");

    const uploaded: OfflineSyncItem[][] = [];
    const uploader: OfflineSyncUploader = {
      upload: async (items) => {
        uploaded.push(items);
        return { applied: items.length, duplicate: 0, insufficient: 0, frozen: false };
      },
    };
    const serviceB = makeService({ ledger: ledgerB, snapshots, now, uploader });
    await serviceB.refresh();
    // 余额 100 − 待补扣 50 = 50 < 60：恢复的记账参与门禁
    assert.throws(
      () => serviceB.reserve("e3", null, "solving", 60),
      (error: unknown) => error instanceof EntitlementError && error.code === "INSUFFICIENT",
    );
    serviceB.reserve("e3", null, "solving", 50);

    // 联网对账：三条待补扣批量上报 → 标记 → 再次重启归零
    const { sync } = await serviceB.reconcile();
    assert.equal(sync?.applied, 3);
    assert.deepEqual(
      uploaded[0]?.map((item) => item.exec_id),
      ["e1", "e2", "e3"],
    );
    const ledgerC = makeLedger(secrets);
    assert.equal(ledgerC.pendingNet(), 0);
    assert.equal(ledgerC.get("e1")?.uploaded, true);

    const serviceC = makeService({ ledger: ledgerC, snapshots, now });
    await serviceC.refresh();
    serviceC.reserve("e4", null, "writing", 100); // 额度恢复：100 − 0
  });
});

// ---------------------------------------------------------------------------
// 真实服务端联调（不可达自动 skip）：离线记账 → 跨重启 → 真机对账补扣 → 账单核销
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

describe("离线账本跨重启联调（真实服务端）", () => {
  it("注册 → 快照（真 JWKS 验签）→ 离线记账 → 重启恢复 → 对账上报真机补扣 → 账单核销", async (t) => {
    if (!(await serverReachable())) {
      t.skip(`服务端不可达（${BASE_URL}），跳过联调`);
      return;
    }
    const email = `ledger-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
    const auth = new CloudAuthBridge({
      baseUrl: BASE_URL,
      fingerprint: newFingerprint(),
      platform: "test",
      timeoutMs: 5000,
    });
    try {
      await auth.register({ username: email, password: "Ledger0Pass" });
    } catch (error) {
      if (error instanceof AuthFailureError && error.reason === "locked") {
        t.skip(`出口 IP 处于防爆破锁定窗口（423），跳过联调：${error.message}`); // 不因环境态红灯
        return;
      }
      throw error;
    }

    // 初始余额以服务端口径为准（注册赠分等额度不硬编码）
    const business = new CloudBusinessBridge({
      baseUrl: BASE_URL,
      getToken: () => auth.getToken(),
      timeoutMs: 5000,
    });
    const initial = await business.billingOverview();

    const secrets = new InMemorySecretStore();
    const encryptor = new XorEncryptor("live-ledger-key");

    // 会话 A：真快照（真 JWKS 验签）→ 离线记账（确认 30）
    const ledgerA = createEncryptedOfflineLedger({ secrets, encryptor });
    const serviceA = createCloudEntitlementService({
      baseUrl: BASE_URL,
      getToken: () => auth.getToken(),
      ledger: ledgerA,
      timeoutMs: 5000,
    });
    await serviceA.refresh();
    const execId = `live-${Date.now()}`;
    serviceA.reserve(execId, "t-live", "analysis", 30);
    serviceA.confirm(execId);

    // 重启：同密文存储重建账本 → 待补扣延续
    const ledgerB = createEncryptedOfflineLedger({ secrets, encryptor });
    assert.equal(ledgerB.pendingNet(), 30);

    // 联网对账：真实 POST /v1/points/offline-sync（批量补扣）+ 快照重建
    const serviceB = createCloudEntitlementService({
      baseUrl: BASE_URL,
      getToken: () => auth.getToken(),
      ledger: ledgerB,
      timeoutMs: 5000,
    });
    const { sync } = await serviceB.reconcile();
    assert.equal(sync?.applied, 1);
    assert.equal(ledgerB.pendingNet(), 0);

    // 账单核销：余额相对初始值 − 30；流水含 -30 离线消耗
    const after = await business.billingOverview();
    assert.equal(after.pointsBalance, initial.pointsBalance - 30);
    const rows = await business.billingLedger();
    assert.ok(rows.some((row) => row.points === -30), `流水应含离线补扣 -30：${JSON.stringify(rows)}`);
  });
});