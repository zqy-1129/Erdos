/**
 * 引擎首行密钥注入时序单测（FE-KEYIN W5 验收）。
 *
 * 红线（DEC-011 / SP3-2）：Key 仅经 stdin 一次传输、写毕即弃引用；
 * 空行 → 无 Key 模式（FakeLLM）。客户端永不写 `ERDOS_NO_KEY` 字面量。
 *
 * inject.ts 仅 import type（node:child_process），无运行时依赖，可直接 node --test。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { ChildProcessWithoutNullStreams } from "node:child_process";

import { buildFirstLine, injectFirstLine, INJECT_READY_TIMEOUT_MS } from "../main/engine-host/inject.ts";

/** 仅捕获 stdin 写入的假子进程（满足注入所需的最小面）。 */
function fakeChild(writes: string[]): ChildProcessWithoutNullStreams {
  return {
    stdin: { write: (s: string): boolean => { writes.push(s); return true; } },
  } as unknown as ChildProcessWithoutNullStreams;
}

describe("引擎首行注入时序（FE-KEYIN W5）", () => {
  it("无 Key（null）→ 空字符串 → 引擎走无 Key 模式（FakeLLM）", () => {
    assert.equal(buildFirstLine(null), "");
  });

  it("空字符串 Key 与 null 等价（都走无 Key 模式）", () => {
    assert.equal(buildFirstLine(""), "");
  });

  it("有 Key → 原样返回明文（仅内存，调用方写毕即弃引用）", () => {
    assert.equal(buildFirstLine("sk-openai-9f2a7c1e"), "sk-openai-9f2a7c1e");
  });

  it("injectFirstLine 有 Key：写入明文 + 单个换行，且仅写一次", () => {
    const writes: string[] = [];
    injectFirstLine(fakeChild(writes), "sk-openai-9f2a7c1e");
    assert.deepEqual(writes, ["sk-openai-9f2a7c1e\n"]);
  });

  it("injectFirstLine 无 Key：也写一个换行，避免引擎读 stdin 首行阻塞", () => {
    const writes: string[] = [];
    injectFirstLine(fakeChild(writes), null);
    assert.deepEqual(writes, ["\n"]);
  });

  it("INJECT_READY_TIMEOUT_MS 就绪判定超时恒为 10s（spawning→ready 判活窗口）", () => {
    assert.equal(INJECT_READY_TIMEOUT_MS, 10_000);
  });

  it("不写 ERDOS_NO_KEY 字面量：空 Key 用空行表达（保留给命令行手工调试）", () => {
    const writes: string[] = [];
    injectFirstLine(fakeChild(writes), null);
    const all = writes.join("");
    assert.ok(!all.includes("ERDOS_NO_KEY"), "客户端注入不得写入 ERDOS_NO_KEY 字面量");
  });
});
