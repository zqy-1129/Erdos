/**
 * Key 连通测试分类单测（FE-PRELOAD W4 / keysTest 真实化）：
 * - 探测成功 → none（含工具模式摘要）；
 * - 探测失败 → network（红线：不判定 Key 无效，不返回 unauthorized/balance）；
 * - 无引擎/无探测函数 → unverified（已保存未检测，防误判「连通成功」）；
 * - 输入非法/探测异常 → invalid/network（携带原因）。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { classifyProbe, keysTestOutcome, UNVERIFIED_OUTCOME, type ProbeFn } from "../main/ipc/key-probe.ts";

const OK_PROBE: ProbeFn = async () => ({
  ok: true,
  models_endpoint: true,
  tool_mode: "tool_loop",
  probe_source: "probe",
});

describe("classifyProbe 探测结果分类", () => {
  it("models_endpoint 200 → 连通（none，附工具模式与来源）", () => {
    const outcome = classifyProbe({ ok: true, models_endpoint: true, tool_mode: "tool_loop", probe_source: "probe" });
    assert.equal(outcome.ok, true);
    assert.equal(outcome.reason, "none");
    assert.match(outcome.detail, /tool_loop/);
    assert.match(outcome.detail, /probe/);
  });

  it("探测失败 → network，且文案明确不判定 Key 无效（红线）", () => {
    const outcome = classifyProbe({ ok: false, models_endpoint: false, tool_mode: "stage_level" });
    assert.equal(outcome.ok, false);
    assert.equal(outcome.reason, "network");
    assert.match(outcome.detail, /不判定 Key 无效/);
  });

  it("null（未探测）→ unverified（不冒充连通成功）", () => {
    const outcome = classifyProbe(null);
    assert.equal(outcome.reason, "unverified");
    assert.equal(outcome.ok, UNVERIFIED_OUTCOME.ok);
    assert.match(outcome.detail, /未发起真实探测/);
  });
});

describe("keysTestOutcome 完整流程", () => {
  it("无探测函数 → unverified（Web/未接线环境）", async () => {
    const outcome = await keysTestOutcome({ key: "sk-x", baseUrl: "https://api.example.com/v1" });
    assert.equal(outcome.reason, "unverified");
  });

  it("Key 为空 → invalid（不保存不探测）", async () => {
    let called = false;
    const probe: ProbeFn = async () => {
      called = true;
      return { ok: true, models_endpoint: true, tool_mode: "stage_level" };
    };
    const outcome = await keysTestOutcome({ key: "   ", baseUrl: "https://api.example.com/v1", probe });
    assert.equal(outcome.reason, "invalid");
    assert.equal(called, false);
  });

  it("Base URL 为空（有探测函数）→ invalid", async () => {
    const outcome = await keysTestOutcome({ key: "sk-x", baseUrl: "  ", probe: OK_PROBE });
    assert.equal(outcome.reason, "invalid");
  });

  it("探测成功 → none，params 透传（model 缺省兜底 unknown-model）", async () => {
    let received: { base_url: string; model: string } | null = null;
    const probe: ProbeFn = async (params) => {
      received = params;
      return { ok: true, models_endpoint: true, tool_mode: "tool_loop", probe_source: "cache" };
    };
    const outcome = await keysTestOutcome({ key: "sk-x", baseUrl: "https://api.example.com/v1", probe });
    assert.equal(outcome.reason, "none");
    assert.deepEqual(received, { base_url: "https://api.example.com/v1", model: "unknown-model" });
  });

  it("模型名显式给出 → 透传", async () => {
    let received: { base_url: string; model: string } | null = null;
    const probe: ProbeFn = async (params) => {
      received = params;
      return { ok: true, models_endpoint: true, tool_mode: "stage_level" };
    };
    await keysTestOutcome({ key: "sk-x", baseUrl: "https://api.example.com/v1", model: "deepseek-chat", probe });
    assert.equal(received!.model, "deepseek-chat");
  });

  it("探测抛错（引擎重启失败/超时）→ network 且携带原因", async () => {
    const probe: ProbeFn = async () => {
      throw new Error("ENGINE_RESTART：引擎不可用");
    };
    const outcome = await keysTestOutcome({ key: "sk-x", baseUrl: "https://api.example.com/v1", probe });
    assert.equal(outcome.ok, false);
    assert.equal(outcome.reason, "network");
    assert.match(outcome.detail, /ENGINE_RESTART/);
  });
});