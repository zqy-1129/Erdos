/**
 * EngineRpcClient 分流单测（FE-HOST W3 验收：stdout 分流 / stderr 脱敏 / 超时 / 关闭）。
 *
 * 纯逻辑测试：用 PassThrough 流伪造子进程 stdio，不依赖真实引擎进程。
 * 覆盖：响应 resolve/reject、事件白名单转发、未知事件拒绝计数、坏行、单行超限、
 * stderr 脱敏、invoke 超时、close 清 pending、无 pending 响应忽略。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { PassThrough } from "node:stream";
import type { ChildProcessWithoutNullStreams } from "node:child_process";

import { EngineRpcClient } from "../main/engine-host/rpc.ts";

interface Harness {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  rpc: EngineRpcClient;
  events: unknown[];
  logs: string[];
  protocolErrors: Error[];
}

function makeClient(): Harness {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const child = { stdin, stdout, stderr } as unknown as ChildProcessWithoutNullStreams;
  const events: unknown[] = [];
  const logs: string[] = [];
  const protocolErrors: Error[] = [];
  const rpc = new EngineRpcClient(child, {
    onEvent: (event) => events.push(event),
    onStderr: (line) => logs.push(line),
    onProtocolError: (err) => protocolErrors.push(err),
  });
  return { stdin, stdout, stderr, rpc, events, logs, protocolErrors };
}

/** 向 stdout 写一行 JSON（模拟引擎响应/事件）。 */
function feedLine(stdout: PassThrough, obj: unknown): void {
  stdout.write(`${JSON.stringify(obj)}\n`);
}

/** 轮询等待条件成立（readline 处理为异步）。 */
async function waitFor(fn: () => boolean, timeoutMs = 1000): Promise<void> {
  const start = Date.now();
  while (!fn()) {
    if (Date.now() - start > timeoutMs) throw new Error("waitFor 超时");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

describe("EngineRpcClient stdout 分流（W3）", () => {
  it("响应行（id+result）resolve 对应 pending 并返回 result", async () => {
    const { stdout, rpc } = makeClient();
    const pending = rpc.invoke("get_status", {});
    feedLine(stdout, { jsonrpc: "2.0", id: 0, result: { engine: "idle" } });
    const res = await pending;
    assert.deepEqual(res, { engine: "idle" });
  });

  it("响应行（id+error）reject 并携带 code/message", async () => {
    const { stdout, rpc } = makeClient();
    const pending = rpc.invoke("start_stage", {});
    feedLine(stdout, { jsonrpc: "2.0", id: 0, error: { code: -32000, message: "boom" } });
    await assert.rejects(pending, /引擎错误 -32000：boom/);
  });

  it("白名单事件行 → onEvent 转发", async () => {
    const { stdout, events } = makeClient();
    const evt = {
      trace_id: "t1", event: "stage.progress", task_id: "x",
      stage: "analysis", progress: 0.5, timestamp: "2026-01-01T00:00:00Z",
    };
    feedLine(stdout, evt);
    await waitFor(() => events.length === 1);
    assert.equal((events[0] as { event: string }).event, "stage.progress");
  });

  it("未知事件 → onProtocolError 且 unknownEventTotal 计数", async () => {
    const { stdout, rpc, protocolErrors } = makeClient();
    feedLine(stdout, { event: "evil.thing", trace_id: "t" });
    await waitFor(() => protocolErrors.length === 1);
    assert.match(protocolErrors[0].message, /未知事件/);
    assert.equal(rpc.unknownEventTotal, 1);
  });

  it("非 JSON 坏行 → onProtocolError", async () => {
    const { stdout, protocolErrors } = makeClient();
    stdout.write("this is not json\n");
    await waitFor(() => protocolErrors.length === 1);
    assert.match(protocolErrors[0].message, /坏行/);
  });

  it("单行超 1MiB → onProtocolError（不解析，EC-S6）", async () => {
    const { stdout, protocolErrors } = makeClient();
    stdout.write(`${"x".repeat(1024 * 1024 + 10)}\n`);
    await waitFor(() => protocolErrors.length === 1);
    assert.match(protocolErrors[0].message, /1MiB/);
  });

  it("无 pending 的响应（id 不匹配）静默忽略", async () => {
    const { stdout, rpc, protocolErrors } = makeClient();
    feedLine(stdout, { jsonrpc: "2.0", id: 999, result: {} });
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(protocolErrors.length, 0);
    assert.equal(rpc.unknownEventTotal, 0);
  });
});

describe("EngineRpcClient 生命周期（W3）", () => {
  it("invoke 超时 → reject（控制类默认 5s，可覆盖短超时）", async () => {
    const { rpc } = makeClient();
    await assert.rejects(rpc.invoke("get_status", {}, 30), /超时/);
  });

  it("close() 清空 pending（以 ENGINE_RESTART reject），后续 invoke 拒绝", async () => {
    const { rpc } = makeClient();
    const pending = rpc.invoke("get_status", {});
    rpc.close();
    await assert.rejects(pending, /ENGINE_RESTART/);
    await assert.rejects(rpc.invoke("get_status", {}), /ENGINE_RESTART/);
  });

  it("stderr 行经 maskText 脱敏后 onStderr（零明文 Key 落日志）", async () => {
    const { stderr, logs } = makeClient();
    stderr.write("key=sk-abcdefgh12345678 leaked\n");
    await waitFor(() => logs.length === 1);
    assert.ok(!logs[0].includes("sk-abcdefgh12345678"), "stderr 不得含明文 Key");
    assert.match(logs[0], /sk-a\.\.\.5678/);
  });
});
