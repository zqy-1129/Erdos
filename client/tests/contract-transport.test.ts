/**
 * stdio 传输契约守护（客户端侧）：contracts/engine-rpc.schema.json `transport` ↔ 主进程实现。
 *
 * UTF-8 + LF 是 Windows 上的 P0 红线：码页错配不报错，只表现为"引擎事件静默丢失"，
 * 而 Linux CI 永远不会暴露它。条款此前只写在 client/main/engine-protocol.md（人读版），
 * 三边都没有机器判据——本文件把它变成可红的测试。
 * 引擎侧同款守护见 engine/tests/test_contract_alignment.py 的 transport 段。
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import { PassThrough } from "node:stream";
import type { ChildProcessWithoutNullStreams } from "node:child_process";

import { EngineRpcClient } from "../main/engine-host/rpc.ts";
import { injectFirstLine } from "../main/engine-host/inject.ts";

const here = dirname(fileURLToPath(import.meta.url));
const contractPath = join(here, "..", "..", "contracts", "engine-rpc.schema.json");

interface Channel {
  direction: string;
  errors: string;
  newline?: string;
}
interface Transport {
  framing: string;
  encoding: string;
  encoding_owner: string;
  enforcement: string;
  channels: Record<"stdin" | "stdout" | "stderr", Channel>;
  limits: { max_line_bytes: number };
}
const transport = (JSON.parse(readFileSync(contractPath, "utf-8")) as { transport: Transport })
  .transport;

interface Harness {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  rpc: EngineRpcClient;
  written: Buffer[];
  events: Record<string, unknown>[];
  protocolErrors: Error[];
}

function makeClient(): Harness {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const written: Buffer[] = [];
  stdin.on("data", (chunk: Buffer) => written.push(chunk));
  const events: Record<string, unknown>[] = [];
  const protocolErrors: Error[] = [];
  const rpc = new EngineRpcClient(
    { stdin, stdout, stderr } as unknown as ChildProcessWithoutNullStreams,
    {
      onEvent: (event) => events.push(event as unknown as Record<string, unknown>),
      onStderr: () => {},
      onProtocolError: (err) => protocolErrors.push(err),
    },
  );
  return { stdin, stdout, stderr, rpc, written, events, protocolErrors };
}

async function waitFor(fn: () => boolean, timeoutMs = 1000): Promise<void> {
  const start = Date.now();
  while (!fn()) {
    if (Date.now() - start > timeoutMs) throw new Error("waitFor 超时");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

describe("stdio 传输契约（客户端侧）", () => {
  it("契约条款自洽：UTF-8 属引擎、NDJSON 行结束符为 LF、协议管道 strict", () => {
    assert.equal(transport.encoding, "utf-8");
    assert.equal(transport.encoding_owner, "engine");
    assert.match(transport.framing, /LF/);
    assert.match(transport.enforcement, /PYTHONIOENCODING/);
    assert.equal(transport.channels.stdout.newline, "\n");
    assert.equal(transport.channels.stdin.errors, "strict");
    assert.equal(transport.channels.stdout.errors, "strict");
    assert.equal(transport.channels.stderr.errors, "replace", "只有不承载协议的 stderr 允许 replace");
  });

  it("主进程写出的请求行：LF 结尾、无 CR、UTF-8 原文（中文不被转义或替换）", async () => {
    const h = makeClient();
    h.rpc.invoke("start_stage", { task_id: "任务-中文", stage: "analysis" }).catch(() => {});
    await waitFor(() => h.written.length > 0);
    const raw = Buffer.concat(h.written);
    const text = raw.toString("utf8");
    assert.ok(text.endsWith("\n"), "每行必须以 LF 结束");
    assert.ok(!text.includes("\r"), "不得出现 CR（CRLF 会让引擎按空行分帧）");
    assert.ok(text.includes("任务-中文"), "中文必须以 UTF-8 原样写出");
    assert.equal(JSON.parse(text).params.task_id, "任务-中文");
    h.rpc.close();
  });

  it("引擎 stdout 的中文事件行按 UTF-8 完整解析（码页错配在这里必然变坏行）", async () => {
    const h = makeClient();
    const line = '{"event":"stage.progress","task_id":"t-1","note":"门禁意见：题面含中文 😀"}\n';
    h.stdout.write(Buffer.from(line, "utf8"));
    await waitFor(() => h.events.length > 0);
    assert.equal(h.events[0].note, "门禁意见：题面含中文 😀");
    assert.equal(h.protocolErrors.length, 0);
    h.rpc.close();
  });

  it("单行上限取自契约 limits.max_line_bytes（EC-S6：超限按协议错误，不解析）", async () => {
    const h = makeClient();
    const padding = "x".repeat(transport.limits.max_line_bytes);
    h.stdout.write(`{"event":"stage.progress","pad":${padding}}\n`);
    await waitFor(() => h.protocolErrors.length > 0);
    assert.match(h.protocolErrors[0].message, /1MiB|超过/);
    assert.equal(h.events.length, 0);
    h.rpc.close();
  });

  it("首行密钥注入同样走 LF（Key 行也是协议帧的一部分）", async () => {
    const h = makeClient();
    injectFirstLine(
      { stdin: h.stdin } as unknown as ChildProcessWithoutNullStreams,
      "sk-test-abcdef",
    );
    await waitFor(() => h.written.length > 0);
    const text = Buffer.concat(h.written).toString("utf8");
    assert.equal(text, "sk-test-abcdef\n");
    h.rpc.close();
  });

  it("主进程源码不设置也不依赖 PYTHONIOENCODING（编码由引擎进程入口负责）", () => {
    const mainDir = join(here, "..", "main");
    const files = readdirSync(mainDir, { recursive: true }).filter(
      (f) => typeof f === "string" && f.endsWith(".ts"),
    );
    assert.ok(files.length > 10, "扫描目录异常，守护失去意义");
    for (const file of files) {
      const source = readFileSync(join(mainDir, file as string), "utf-8");
      assert.ok(
        !source.includes("PYTHONIOENCODING"),
        `${String(file)} 不得通过 PYTHONIOENCODING 兜底编码`,
      );
    }
  });
});
