/**
 * 契约三边一致性守护（客户端侧）：contracts/engine-rpc.schema.json ↔ 客户端 IPC 类型。
 * RPC_METHODS / ENGINE_EVENT_NAMES（shared/ipc.ts 运行时来源）与 schema 零漂移；
 * 引擎侧同款守护见 engine/tests/test_contract_alignment.py。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { ENGINE_EVENT_NAMES, RPC_METHODS } from "../shared/ipc.ts";

const schemaPath = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "contracts",
  "engine-rpc.schema.json",
);

interface RpcSchema {
  methods: Record<string, unknown>;
  events: Record<string, unknown>;
}

const schema = JSON.parse(readFileSync(schemaPath, "utf-8")) as RpcSchema;

describe("引擎 IPC 契约三边一致（客户端侧）", () => {
  it("RPC 方法名与 schema methods 完全一致（6 个）", () => {
    assert.deepEqual([...RPC_METHODS].sort(), Object.keys(schema.methods).sort());
    assert.equal(RPC_METHODS.length, 6);
  });

  it("事件名与 schema events 完全一致（5 个，CT-V2 增量后）", () => {
    assert.deepEqual([...ENGINE_EVENT_NAMES].sort(), Object.keys(schema.events).sort());
    assert.equal(ENGINE_EVENT_NAMES.length, 5);
  });

  it("白名单通道覆盖全部 RPC（preload 桥只暴露注册通道）", async () => {
    const { ENGINE_IPC_CHANNELS } = await import("../shared/ipc.ts");
    for (const method of RPC_METHODS) {
      assert.ok(ENGINE_IPC_CHANNELS.includes(`engine:${method}` as never), `缺通道 engine:${method}`);
    }
    // 事件转发通道
    assert.ok(ENGINE_IPC_CHANNELS.includes("engine:event" as never));
  });
});