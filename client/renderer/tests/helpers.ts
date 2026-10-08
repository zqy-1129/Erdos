/**
 * 组件测试公共设施（SP3-4）：fake bridge（通道→结果映射 / 可选报错通道 / 调用留痕）。
 */

import type { ErdosBridge } from "../src/bridges/bridge.ts";

/** 桥调用留痕（断言 payload 用）。 */
export interface FakeBridgeCall {
  channel: string;
  payload: unknown;
}

export interface FakeBridgeOptions {
  /** channel → invoke 结果。 */
  results?: Record<string, unknown>;
  /** 命中即 reject（模拟异常/网络错误）。 */
  failChannels?: Set<string>;
  /** 传入数组即记录每次 invoke（channel + payload）。 */
  calls?: FakeBridgeCall[];
}

export function fakeBridge(options: FakeBridgeOptions = {}): ErdosBridge {
  const results = options.results ?? {};
  const failChannels = options.failChannels ?? new Set<string>();
  return {
    invoke: async <T = unknown>(channel: string, payload?: unknown): Promise<T> => {
      options.calls?.push({ channel, payload });
      if (failChannels.has(channel)) {
        throw new Error(`模拟网络错误（${channel}）：fetch failed`);
      }
      return results[channel] as T;
    },
    subscribe: () => () => {},
  };
}