/**
 * Key 连通测试分类（FE-PRELOAD W4 / FE-TOOLUI W12）：keysTest 真实化的纯逻辑层。
 *
 * 数据来源与红线：
 * - 探测经引擎 provider_test（EN-CAP）：GET {base_url}/models，200 视为连通（models_endpoint）；
 * - 红线（《Erdos_开发文档》§8.2 / capabilities.py）：探测失败**不得判定 Key 无效**——
 *   失败一律如实归类为「端点不可达/路径不正确」，不返回 unauthorized/balance；
 * - 无引擎（未接线/Web 演示环境）：返回 unverified（已保存未检测），
 *   不冒充「连通成功」（方案 §4.2：防误判已连通）。
 *
 * 本模块不依赖 electron 运行时，由 node:test 直接单测；
 * BridgeBackend.keysTest 负责把引擎探测函数注入进来。
 */

/** 引擎 provider_test 应答（对齐 shared/ipc.ts ProviderTestResult）。 */
export interface ProbeResult {
  ok: boolean;
  models_endpoint: boolean;
  tool_mode: string;
  probe_source?: string;
}

/** 探测函数（主进程接线：EngineHost.reloadKey → invoke provider_test）。 */
export type ProbeFn = (params: { base_url: string; model: string }) => Promise<ProbeResult>;

/** 与渲染层 KeyTestResult 结构一致（避免跨层 import；reason 增补 unverified）。 */
export interface KeyTestOutcome {
  ok: boolean;
  reason: "none" | "unauthorized" | "network" | "balance" | "invalid" | "unverified";
  detail: string;
}

/** 无引擎时的统一结论：已保存、未检测（文案显式声明，不显示「连通成功」）。 */
export const UNVERIFIED_OUTCOME: KeyTestOutcome = {
  ok: true,
  reason: "unverified",
  detail: "已加密保存；当前环境未连接引擎，未发起真实探测",
};

/** 探测应答 → 可读结论（null=未探测）。 */
export function classifyProbe(result: ProbeResult | null): KeyTestOutcome {
  if (result === null) return { ...UNVERIFIED_OUTCOME };
  if (result.ok) {
    const source = result.probe_source ? `；探测来源：${result.probe_source}` : "";
    return {
      ok: true,
      reason: "none",
      detail: `端点连通正常（/models 200）；工具模式：${result.tool_mode}${source}`,
    };
  }
  return {
    ok: false,
    reason: "network",
    detail: "端点探测未通过（/models 未返回 200）：请检查 Base URL 与网络；按红线不判定 Key 无效",
  };
}

/**
 * keysTest 完整流程（校验 → 探测 → 分类）：
 * - Key 为空 → invalid（不保存不探测）；
 * - 无探测函数 → unverified；
 * - 探测异常（引擎重启失败/超时）→ network（携带原因，不误判 Key）。
 */
export async function keysTestOutcome(options: {
  key: string;
  baseUrl: string;
  /** 探测用模型名（provider_test 必填；仅参与引擎能力缓存键，不参与 HTTP 探测）。 */
  model?: string;
  probe?: ProbeFn;
}): Promise<KeyTestOutcome> {
  const key = (options.key ?? "").trim();
  if (!key) return { ok: false, reason: "invalid", detail: "Key 不能为空" };
  if (!options.probe) return { ...UNVERIFIED_OUTCOME };
  const baseUrl = (options.baseUrl ?? "").trim();
  if (!baseUrl) return { ok: false, reason: "invalid", detail: "Base URL 不能为空" };
  const model = (options.model ?? "").trim() || "unknown-model";
  try {
    const result = await options.probe({ base_url: baseUrl, model });
    return classifyProbe(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, reason: "network", detail: `探测未完成：${message}` };
  }
}