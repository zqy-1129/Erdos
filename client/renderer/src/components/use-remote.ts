/**
 * 远端数据加载 hook（SP3-4）：统一每页 加载/空/异常/断网 四态数据源。
 * errorKind 区分网络类错误（断网态 UI）与业务/服务错误（异常态 UI）。
 */

import { useCallback, useEffect, useState } from "react";

export type RemoteErrorKind = "network" | "other";

export interface RemoteData<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  errorKind: RemoteErrorKind | null;
  reload(): void;
}

function classify(error: unknown): { message: string; kind: RemoteErrorKind } {
  const message = error instanceof Error ? error.message : String(error);
  const kind: RemoteErrorKind = /网络|network|fetch|ECONN|timeout|超时/i.test(message) ? "network" : "other";
  return { message, kind };
}

export function useRemoteData<T>(loader: () => Promise<T>, dependencies: readonly unknown[] = []): RemoteData<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; kind: RemoteErrorKind } | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((value) => {
        if (cancelled) return;
        setData(value);
        setError(null);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(classify(reason));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick, ...dependencies]);

  return { data, loading, error: error?.message ?? null, errorKind: error?.kind ?? null, reload };
}
