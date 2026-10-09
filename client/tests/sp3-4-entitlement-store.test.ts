/**
 * 权益本地状态安全落盘测试（SP3-4 第二批）：
 * 只存密文/读写往返/跨实例持久化/清盘语义/形状校验与 fail-safe、
 * 跨重启 hydration（宽限基准 + 防重放门 + 设备计数器延续）、写穿语义与 reset（会话级缓存）。
 */
import assert from "node:assert/strict";
import { generateKeyPairSync, sign as edSign } from "node:crypto";
import { describe, it } from "node:test";

import {
  EntitlementService,
  InMemoryEntitlementStateStore,
  parseStoredEntitlementState,
  type EntitlementStateStore,
  type StoredEntitlementState,
} from "../main/entitlement/service.ts";
import { InMemoryOfflineLedger, type OfflineLedger } from "../main/entitlement/offline-ledger.ts";
import {
  ENTITLEMENT_SCOPE,
  createEncryptedEntitlementStateStore,
} from "../main/entitlement/state-store.ts";
import type { EntitlementPayload, EntitlementSnapshot } from "../main/entitlement/types.ts";
import { EntitlementError } from "../main/entitlement/types.ts";
import { canonicalBytes } from "../main/entitlement/verify.ts";
import { InMemorySecretStore, XorEncryptor } from "../main/key-vault.ts";

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

/** 注入存储的服务装配（fetcher 队列；不触网）。 */
function makeService(options: {
  store?: EntitlementStateStore;
  snapshots?: EntitlementSnapshot[];
  now: () => number;
  ledger?: OfflineLedger;
}): { service: EntitlementService; snapshots: EntitlementSnapshot[] } {
  const snapshots = options.snapshots ?? [];
  return {
    snapshots,
    service: new EntitlementService({
      fetcher: { fetch: async () => snapshots.shift() as EntitlementSnapshot },
      keyResolver: { resolve: async (kid) => (kid === "v1" ? PUBLIC_DER : null) },
      uploader: {
        upload: async (items) => ({ applied: items.length, duplicate: 0, insufficient: 0, frozen: false }),
      },
      store: options.store,
      ledger: options.ledger,
      clock: options.now,
      graceMs: GRACE_MS,
    }),
  };
}

function makeStore(secrets = new InMemorySecretStore()): {
  secrets: InMemorySecretStore;
  store: EntitlementStateStore;
} {
  return {
    secrets,
    store: createEncryptedEntitlementStateStore({ secrets, encryptor: new XorEncryptor("test-key") }),
  };
}

function sampleState(): StoredEntitlementState {
  return {
    snapshot: makeSnapshot(BASE),
    last_sync_at_ms: BASE + 5,
    last_issued_at_ms: BASE,
    device_counter: 3,
  };
}

// ---------------------------------------------------------------------------
// 落盘载荷形状校验
// ---------------------------------------------------------------------------

describe("parseStoredEntitlementState 形状校验", () => {
  it("合法状态原样通过（快照 + 三项同步元数据）", () => {
    const state = sampleState();
    assert.deepEqual(parseStoredEntitlementState(state), state);
  });

  it("载荷字段漂移（类型错）→ null；非对象 → null", () => {
    const drifted = sampleState();
    assert.equal(
      parseStoredEntitlementState({
        ...drifted,
        snapshot: { ...drifted.snapshot, payload: { ...drifted.snapshot.payload, purchased_balance: "100" } },
      }),
      null,
    );
    assert.equal(parseStoredEntitlementState({ ...drifted, snapshot: { ...drifted.snapshot, signature: 1 } }), null);
    assert.equal(parseStoredEntitlementState(null), null);
    assert.equal(parseStoredEntitlementState("json"), null);
  });

  it("同步元数据缺失/非数 → null（防半可信状态被当真）", () => {
    const withoutSync = { ...sampleState() } as Record<string, unknown>;
    delete withoutSync["last_sync_at_ms"];
    assert.equal(parseStoredEntitlementState(withoutSync), null);
    assert.equal(parseStoredEntitlementState({ ...sampleState(), device_counter: "3" }), null);
    assert.equal(parseStoredEntitlementState({ ...sampleState(), last_issued_at_ms: Number.NaN }), null);
  });
});

// ---------------------------------------------------------------------------
// 加密落盘
// ---------------------------------------------------------------------------

describe("权益状态加密落盘", () => {
  it("只存密文：读写往返不落明文，scope 为 entitlement.state", () => {
    const { secrets, store } = makeStore();
    store.save(sampleState());
    const ciphertext = secrets.get(ENTITLEMENT_SCOPE);
    assert.ok(ciphertext && ciphertext.length > 0);
    assert.ok(!ciphertext.includes("signature"), "密文不得包含明文片段");
    assert.ok(!ciphertext.includes(String(BASE)), "密文不得包含明文时间戳");
    assert.deepEqual(store.load(), sampleState());
  });

  it("跨实例持久化（模拟重启）：同一密文存储新实例可恢复状态", () => {
    const secrets = new InMemorySecretStore();
    makeStore(secrets).store.save(sampleState());
    const reopened = createEncryptedEntitlementStateStore({
      secrets,
      encryptor: new XorEncryptor("test-key"),
    });
    assert.deepEqual(reopened.load(), sampleState());
  });

  it("清盘语义：clear() 删除密文，load 返回 null（登录/登出换账号）", () => {
    const { secrets, store } = makeStore();
    store.save(sampleState());
    store.clear();
    assert.equal(secrets.get(ENTITLEMENT_SCOPE), null);
    assert.equal(store.load(), null);
  });

  it("fail-safe：密文损坏 / 明文非 JSON / 形状漂移 → 一律 null（回退未同步）", () => {
    const { secrets, store } = makeStore();
    secrets.set(ENTITLEMENT_SCOPE, "!!!not-base64-ciphertext###");
    assert.equal(store.load(), null);

    secrets.set(ENTITLEMENT_SCOPE, new XorEncryptor("test-key").encrypt("{ 不是 JSON"));
    assert.equal(store.load(), null);

    secrets.set(ENTITLEMENT_SCOPE, new XorEncryptor("test-key").encrypt(JSON.stringify({ snapshot: {} })));
    assert.equal(store.load(), null);
  });

  it("scope 可注入且与密钥/会话空间隔离（互不覆盖）", () => {
    const { secrets, store } = makeStore();
    secrets.set("session.tokens", "session-ciphertext");
    store.save(sampleState());
    assert.equal(secrets.get("session.tokens"), "session-ciphertext");
    assert.deepEqual(store.load(), sampleState());

    const scoped = createEncryptedEntitlementStateStore({
      secrets,
      encryptor: new XorEncryptor("test-key"),
      scope: "custom.entitlement",
    });
    assert.equal(scoped.load(), null); // 不同 scope 不串读
  });
});

// ---------------------------------------------------------------------------
// 跨重启 hydration（本批核心：离线宽限跨重启）
// ---------------------------------------------------------------------------

describe("权益状态跨重启延续", () => {
  it("重启后宽限倒计时按落盘同步时间延续（1h 后重启仍 ready，剩余 71h）", async () => {
    const secrets = new InMemorySecretStore();
    let now = BASE;
    const seed = makeService({ store: makeStore(secrets).store, snapshots: [makeSnapshot(BASE)], now: () => now });
    await seed.service.refresh();
    assert.equal(seed.service.status(), "ready");

    now = BASE + 3600_000; // 重启：离线 1h
    const restored = makeService({ store: makeStore(secrets).store, now: () => now }).service;
    assert.equal(restored.status(), "ready");
    assert.equal(restored.lastSyncAt(), BASE);
    assert.equal(restored.graceRemainingMs(), GRACE_MS - 3600_000);
    assert.equal(restored.snapshot()?.payload.purchased_balance, 100);
  });

  it("重启后超过 72h → grace_expired（剩余为负），快照仍保留供展示", async () => {
    const secrets = new InMemorySecretStore();
    let now = BASE;
    await makeService({ store: makeStore(secrets).store, snapshots: [makeSnapshot(BASE)], now: () => now }).service.refresh();

    now = BASE + GRACE_MS + 1;
    const restored = makeService({ store: makeStore(secrets).store, now: () => now }).service;
    assert.equal(restored.status(), "grace_expired");
    assert.equal(restored.graceRemainingMs(), -1);
    assert.equal(restored.snapshot()?.payload.purchased_balance, 100);
  });

  it("防重放门延续：重启后拒绝 issued_at 不更新的旧快照（SNAPSHOT_REPLAY）", async () => {
    const secrets = new InMemorySecretStore();
    const now = () => BASE;
    await makeService({ store: makeStore(secrets).store, snapshots: [makeSnapshot(BASE)], now }).service.refresh();

    const restored = makeService({ store: makeStore(secrets).store, snapshots: [makeSnapshot(BASE)], now });
    await assert.rejects(
      () => restored.service.refresh(),
      (e: unknown) => e instanceof EntitlementError && e.code === "SNAPSHOT_REPLAY",
    );
    assert.equal(restored.service.snapshot()?.payload.purchased_balance, 100, "旧快照应保留");
  });

  it("设备计数器延续：重启后再次成功应用快照 → 计数从落盘值递增", async () => {
    const secrets = new InMemorySecretStore();
    const now = () => BASE;
    const seed = makeService({ store: makeStore(secrets).store, snapshots: [makeSnapshot(BASE)], now }).service;
    await seed.refresh();
    assert.equal(seed.counter(), 1);

    const restored = makeService({
      store: makeStore(secrets).store,
      snapshots: [makeSnapshot(BASE + 1000)],
      now,
    }).service;
    assert.equal(restored.counter(), 1, "重建时应回填落盘计数");
    await restored.refresh();
    assert.equal(restored.counter(), 2);
  });

  it("全新启动（无落盘状态）→ empty，不伪造可用态", () => {
    const service = makeService({ store: makeStore().store, now: () => BASE }).service;
    assert.equal(service.status(), "empty");
    assert.equal(service.snapshot(), null);
    assert.equal(service.graceRemainingMs(), null);
  });

  it("写穿语义：落盘失败 → refresh 抛错且内存态不提交（下轮重试）", async () => {
    class FailingStore implements EntitlementStateStore {
      load(): StoredEntitlementState | null {
        return null;
      }
      save(): void {
        throw new Error("磁盘写入失败");
      }
      clear(): void {}
    }
    const service = makeService({ store: new FailingStore(), snapshots: [makeSnapshot(BASE)], now: () => BASE }).service;
    await assert.rejects(() => service.refresh(), /磁盘写入失败/);
    assert.equal(service.status(), "empty");
    assert.equal(service.counter(), 0);
    assert.equal(service.snapshot(), null);
  });

  it("reset（会话级缓存）：清空落盘与内存态，需联网刷新重建", async () => {
    const { secrets, store } = makeStore();
    const service = makeService({ store, snapshots: [makeSnapshot(BASE)], now: () => BASE }).service;
    await service.refresh();
    assert.equal(service.counter(), 1);

    service.reset();
    assert.equal(service.status(), "empty");
    assert.equal(service.snapshot(), null);
    assert.equal(service.counter(), 0);
    assert.equal(service.graceRemainingMs(), null);
    assert.equal(secrets.get(ENTITLEMENT_SCOPE), null, "密文应一并清除");
  });

  it("reset 同时清空离线流水账本（防换账号残留旧账号待补扣）", async () => {
    const ledger = new InMemoryOfflineLedger();
    const service = makeService({
      store: makeStore().store,
      snapshots: [makeSnapshot(BASE)],
      now: () => BASE,
      ledger,
    }).service;
    await service.refresh();
    service.reserve("exec-1", null, "analysis", 5);
    assert.equal(ledger.pendingItems().length, 1);

    service.reset();
    assert.equal(ledger.pendingItems().length, 0);
    assert.equal(ledger.pendingNet(), 0);
  });

  it("内存存储（缺省/降级）语义与加密存储一致（写入后可读、跨服务实例不延续）", async () => {
    const store = new InMemoryEntitlementStateStore();
    const service = makeService({ store, snapshots: [makeSnapshot(BASE)], now: () => BASE }).service;
    await service.refresh();
    assert.equal(service.status(), "ready");
    const fresh = makeService({ now: () => BASE }).service;
    assert.equal(fresh.status(), "empty", "未注入存储的新实例不应看到旧状态");
  });
});