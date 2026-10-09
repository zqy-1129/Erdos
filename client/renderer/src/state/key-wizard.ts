/**
 * Key 连通测试向导状态机（SP3-4 / US-002）：纯逻辑 reducer（node:test 单测）。
 * 流程：输入（Base URL + Key）→ 保存（主进程加密）→ 连通测试 →
 * 结果分类（401/网络/余额/成功，可读原因）→ 完成/错误。
 */

import type { KeyTestResult } from "../bridges/bridge.ts";

export type WizardStage = "input" | "saving" | "testing" | "done" | "error";

export interface KeyWizardState {
  stage: WizardStage;
  alias: string;
  baseUrl: string;
  key: string;
  /** 探测用模型名（provider_test 必填参数；缺省由主进程兜底 unknown-model）。 */
  model: string;
  result: KeyTestResult | null;
  error: string | null;
}

export const initialWizard: KeyWizardState = {
  stage: "input",
  alias: "",
  baseUrl: "",
  key: "",
  model: "",
  result: null,
  error: null,
};

export type KeyWizardAction =
  | { type: "field"; field: "alias" | "baseUrl" | "key" | "model"; value: string }
  | { type: "save" }
  | { type: "saved" }
  | { type: "testing" }
  | { type: "test-result"; result: KeyTestResult }
  | { type: "fail"; message: string }
  | { type: "reset" };

export function keyWizardReducer(state: KeyWizardState, action: KeyWizardAction): KeyWizardState {
  switch (action.type) {
    case "field":
      return { ...state, [action.field]: action.value, error: null };
    case "save":
      return { ...state, stage: "saving", error: null };
    case "saved":
      return { ...state, stage: "testing" };
    case "testing":
      return { ...state, stage: "testing" };
    case "test-result":
      return { ...state, stage: "done", result: action.result };
    case "fail":
      return { ...state, stage: "error", error: action.message };
    case "reset":
      return { ...state, ...initialWizard, alias: state.alias, baseUrl: state.baseUrl, model: state.model };
    default:
      return state;
  }
}

/** 测试结果 → 可读原因文案（US-002：401/网络/余额 可读）。 */
export function reasonLabel(reason: KeyTestResult["reason"]): string {
  switch (reason) {
    case "unauthorized":
      return "鉴权失败（401）";
    case "network":
      return "网络不可达";
    case "balance":
      return "账户余额不足";
    case "invalid":
      return "输入不合法";
    case "unverified":
      return "已保存（未检测连通）";
    case "none":
      return "连通成功";
  }
}