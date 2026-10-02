/**
 * 自研轻量状态管理（SP3-4）：createStore + useStore（React useSyncExternalStore 绑定）。
 *
 * 约束（执行计划）：状态自研轻量、不引外部状态库；
 * 不变性约定：state 按不可变处理，setState 浅合并生成新引用，
 * 保证 useSyncExternalStore 的 getSnapshot 返回稳定引用（无引用变化不触发重渲染）。
 */

import { useSyncExternalStore } from "react";

export interface Store<T> {
  getState(): T;
  /** 浅合并补丁（必须产生新顶层引用）。 */
  setState(patch: Partial<T>): void;
  subscribe(listener: () => void): () => void;
}

export function createStore<T extends object>(initial: T): Store<T> {
  let state: T = initial;
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    setState(patch: Partial<T>) {
      state = { ...state, ...patch };
      for (const listener of [...listeners]) listener();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** 组件订阅整个 state（顶层引用稳定 → 按需用 useMemo/派生选择）。 */
export function useStore<T extends object>(store: Store<T>): T {
  return useSyncExternalStore(store.subscribe, store.getState);
}