/**
 * 自研 hash 路由（SP3-4）：轻量、无路由库依赖；整页切换仅此一处。
 */

import { useEffect, useState } from "react";

export function useHashRoute(): string {
  const [route, setRoute] = useState(() => normalize(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(normalize(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

function normalize(hash: string): string {
  const path = hash.startsWith("#") ? hash.slice(1) : hash;
  return path === "" ? "/" : path;
}

export function navigate(path: string): void {
  if (normalize(window.location.hash) !== path) {
    window.location.hash = path;
  }
}