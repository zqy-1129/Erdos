/**
 * 用量估算测试（F-002）：token 解析兼容三形态、单价估算、未知模型诚实标注。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { estimateUsage, parseTokenUsage } from "../shared/usage.ts";

describe("token 用量解析", () => {
  it("OpenAI 风格（prompt/completion）", () => {
    assert.deepEqual(parseTokenUsage({ prompt_tokens: 100, completion_tokens: 50 }), {
      inputTokens: 100,
      outputTokens: 50,
      totalTokens: 150,
      splitKnown: true,
    });
  });

  it("Anthropic 风格（input/output）与总量字段", () => {
    assert.deepEqual(parseTokenUsage({ input_tokens: 10, output_tokens: 20 }), {
      inputTokens: 10,
      outputTokens: 20,
      totalTokens: 30,
      splitKnown: true,
    });
    assert.deepEqual(parseTokenUsage({ tokens: 120 }), {
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 120,
      splitKnown: false, // 总量-only：无法拆分输入/输出
    });
  });

  it("缺失/非法字段按 0 处理", () => {
    assert.deepEqual(parseTokenUsage(null), {
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
      splitKnown: false,
    });
    assert.deepEqual(parseTokenUsage({ prompt_tokens: -5 }), {
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
      splitKnown: false,
    });
  });
});

describe("用量估算汇总", () => {
  it("内置单价模型按 1K tokens 计价（deepseek-chat 0.2/0.8 分）", () => {
    const estimate = estimateUsage([
      {
        event_type: "model_call",
        detail: { model: "deepseek-chat", usage: { prompt_tokens: 1000, completion_tokens: 1000 } },
      },
    ]);
    assert.equal(estimate.totalTokens, 2000);
    assert.equal(estimate.modelCalls, 1);
    assert.equal(estimate.ratedCalls, 1);
    assert.equal(estimate.estimatedCostCents, 1.0); // 1000*0.2 + 1000*0.8 = 1 分
  });

  it("总量-only 用量：token 统计在、费用不臆测分摊（总额 null）", () => {
    const estimate = estimateUsage([
      { event_type: "model_call", detail: { model: "deepseek-chat", usage: { tokens: 500 } } },
    ]);
    assert.equal(estimate.totalTokens, 500);
    assert.equal(estimate.modelCalls, 1);
    assert.equal(estimate.ratedCalls, 0); // 无法拆分 → 即使有单价也不计价
    assert.equal(estimate.estimatedCostCents, null);
  });

  it("未知单价模型：给出 token 统计但费用为 null（不误导）", () => {
    const estimate = estimateUsage([
      { event_type: "model_call", detail: { model: "mystery-model", usage: { tokens: 500 } } },
    ]);
    assert.equal(estimate.totalTokens, 500);
    assert.deepEqual(estimate.models, ["mystery-model"]);
    assert.equal(estimate.estimatedCostCents, null);
    assert.equal(estimate.ratedCalls, 0);
  });

  it("混合模型：存在未定价调用时总额置 null 且 ratedCalls 计数", () => {
    const estimate = estimateUsage([
      { event_type: "model_call", detail: { model: "deepseek-chat", usage: { prompt_tokens: 1000, completion_tokens: 0 } } },
      { event_type: "model_call", detail: { model: "other", usage: { tokens: 10 } } },
      { event_type: "tool_call", detail: { tool: "plot" } }, // 非模型事件忽略
    ]);
    assert.equal(estimate.modelCalls, 2);
    assert.equal(estimate.ratedCalls, 1);
    assert.equal(estimate.estimatedCostCents, null);
    assert.ok(estimate.models.includes("deepseek-chat"));
  });
});