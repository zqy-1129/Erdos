/**
 * 用量估算（F-002 Key 管理能力项）：从本地留痕 model_call 事件统计 token 与费用。
 *
 * 口径（诚实原则）：
 * - token 数只来自留痕（SP1-6 usage 字段），兼容 OpenAI（prompt/completion）与
 *   Anthropic（input/output）风格及总量字段；
 * - 费用估算仅对内置单价的模型给出（未知模型返回 null 且明确标注不估算）；
 * - 跨端纯逻辑（主进程读留痕 / 渲染层预览共用），数据源为本地留痕，永不上传。
 */

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  /** 是否已知输入/输出拆分（总量-only 无法参与分桶计价）。 */
  splitKnown: boolean;
}

export interface UsageEstimate {
  modelCalls: number;
  models: string[];
  totalInputTokens: number;
  totalOutputTokens: number;
  totalTokens: number;
  /** 分（人民币）；存在未定价模型的调用时为 null（不给出误导性总额）。 */
  estimatedCostCents: number | null;
  /** 参与费用估算的模型调用数（与 modelCalls 一致才算完整估算）。 */
  ratedCalls: number;
}

/** 内置单价：分（人民币）/1K tokens。
 * DeepSeek 官方公开价（元/百万 tokens）：chat 输入 2/输出 8、reasoner 输入 4/输出 16。
 */
const MODEL_RATES: Record<string, { input: number; output: number }> = {
  "deepseek-chat": { input: 0.2, output: 0.8 },
  "deepseek-reasoner": { input: 0.4, output: 1.6 },
};

/** 兼容解析 usage 详情（OpenAI / Anthropic / 总量三种形态）。 */
export function parseTokenUsage(usage: unknown): TokenUsage {
  if (typeof usage !== "object" || usage === null) {
    return { inputTokens: 0, outputTokens: 0, totalTokens: 0, splitKnown: false };
  }
  const u = usage as Record<string, unknown>;
  const num = (key: string): number => {
    const value = u[key];
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value) : 0;
  };
  const input = num("prompt_tokens") || num("input_tokens");
  const output = num("completion_tokens") || num("output_tokens");
  const splitKnown = input > 0 || output > 0;
  const total = num("total_tokens") || num("tokens") || input + output;
  return {
    inputTokens: input,
    outputTokens: output,
    totalTokens: total,
    splitKnown,
  };
}

export interface UsageEvent {
  event_type: string;
  detail: Record<string, unknown>;
}

/** 汇总一批留痕事件的用量估算（纯函数）。 */
export function estimateUsage(events: UsageEvent[]): UsageEstimate {
  let totalInput = 0;
  let totalOutput = 0;
  let totalTokens = 0;
  let modelCalls = 0;
  let ratedCalls = 0;
  let cost = 0;
  const models = new Set<string>();

  for (const event of events) {
    if (event.event_type !== "model_call") continue;
    modelCalls += 1;
    const model = typeof event.detail["model"] === "string" ? (event.detail["model"] as string) : "";
    if (model) models.add(model);
    const usage = parseTokenUsage(event.detail["usage"]);
    totalInput += usage.inputTokens;
    totalOutput += usage.outputTokens;
    totalTokens += usage.totalTokens;
    const rate = model ? MODEL_RATES[model] : undefined;
    // 仅「已知输入/输出拆分且模型有内置单价」参与计价（总量-only 不臆测分摊）
    if (rate && usage.splitKnown) {
      ratedCalls += 1;
      cost += (usage.inputTokens * rate.input + usage.outputTokens * rate.output) / 1000;
    }
  }

  return {
    modelCalls,
    models: [...models],
    totalInputTokens: totalInput,
    totalOutputTokens: totalOutput,
    totalTokens,
    estimatedCostCents: ratedCalls === modelCalls && modelCalls > 0
      ? Math.round(cost * 100) / 100
      : null,
    ratedCalls,
  };
}