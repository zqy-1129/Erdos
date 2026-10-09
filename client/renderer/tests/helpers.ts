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
  return fakeBridgeHandle(options).bridge;
}

/** fake bridge 句柄：除桥本体外支持事件投递（emit）与调用留痕，供事件型通道测试。 */
export interface FakeBridgeHandle {
  bridge: ErdosBridge;
  calls: FakeBridgeCall[];
  /** 向该通道订阅者投递事件（模拟主进程推送）。 */
  emit(channel: string, payload: unknown): void;
}

export function fakeBridgeHandle(options: FakeBridgeOptions = {}): FakeBridgeHandle {
  const results = options.results ?? {};
  const failChannels = options.failChannels ?? new Set<string>();
  const calls = options.calls ?? [];
  const subscribers = new Map<string, Set<(payload: unknown) => void>>();
  return {
    calls,
    bridge: {
      invoke: async <T = unknown>(channel: string, payload?: unknown): Promise<T> => {
        calls.push({ channel, payload });
        if (failChannels.has(channel)) {
          throw new Error(`模拟网络错误（${channel}）：fetch failed`);
        }
        return results[channel] as T;
      },
      subscribe(channel: string, handler: (payload: unknown) => void): () => void {
        const set = subscribers.get(channel) ?? new Set<(payload: unknown) => void>();
        set.add(handler);
        subscribers.set(channel, set);
        return () => {
          set.delete(handler);
        };
      },
    },
    emit(channel: string, payload: unknown): void {
      for (const handler of [...(subscribers.get(channel) ?? [])]) handler(payload);
    },
  };
}