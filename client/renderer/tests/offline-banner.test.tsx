/**
 * 断网横幅测试（SP3-4 / US-006）：断网宽限倒计时与快照余额、
 * 超 72h 明确提示（在线/断网均不吞没）、冻结提示、同步失败轻提示、正常在线不渲染。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { OfflineBanner } from "../src/components/offline-banner.tsx";
import type { ConnectivityState, EntitlementState } from "../src/state/app-stores.ts";
import { createStore } from "../src/storage/store.ts";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function makeStores(connectivity: ConnectivityState, entitlement: EntitlementState) {
  return {
    connectivity: createStore<ConnectivityState>(connectivity),
    entitlement: createStore<EntitlementState>(entitlement),
  };
}

const READY: EntitlementState = {
  status: "ready",
  balance: 93,
  graceDeadlineMs: Date.now() + 72 * 3600 * 1000,
  stale: false,
};

describe("OfflineBanner 断网态分级提示", () => {
  it("在线且权益就绪：不渲染", () => {
    const { container } = render(
      <OfflineBanner {...makeStores({ online: true }, READY)} />,
    );
    expect(container.querySelector(".offline-banner")).toBeNull();
  });

  it("断网：展示快照余额与 72h 宽限倒计时（每秒递减）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000_000_000);
    act(() => {
      render(
        <OfflineBanner
          {...makeStores({ online: false }, { ...READY, graceDeadlineMs: 1_000_000_000_000 + 72 * 3600 * 1000 })}
        />,
      );
    });
    expect(document.body.textContent).toContain("已断网");
    expect(document.body.textContent).toContain("快照余额 93 分");
    expect(document.body.textContent).toContain("72:00:00");
    // 时间推进 1s → 倒计时递减（71:59:5x）
    act(() => {
      vi.setSystemTime(1_000_000_001_000);
      vi.advanceTimersByTime(1000);
    });
    expect(document.body.textContent).toContain("71:59:5");
  });

  it("宽限超 72h：明确提示非静默失败", () => {
    const { getByText } = render(
      <OfflineBanner
        {...makeStores({ online: true }, { ...READY, status: "grace_expired" })}
      />,
    );
    expect(getByText(/离线宽限已超 72 小时/)).toBeTruthy();
  });

  it("冻结：欠费提示", () => {
    const { getByText } = render(
      <OfflineBanner
        {...makeStores({ online: true }, { ...READY, status: "frozen" })}
      />,
    );
    expect(getByText(/欠费已冻结/)).toBeTruthy();
  });

  it("同步失败：轻提示展示上次快照", () => {
    const { getByText } = render(
      <OfflineBanner
        {...makeStores({ online: true }, { ...READY, stale: true })}
      />,
    );
    expect(getByText(/权益信息同步失败/)).toBeTruthy();
  });

  it("断网且宽限已超：危险提示不被「已断网」轻提示吞没", () => {
    const { getByText, container } = render(
      <OfflineBanner
        {...makeStores({ online: false }, { ...READY, status: "grace_expired" })}
      />,
    );
    expect(getByText(/已断网：离线宽限已超 72 小时/)).toBeTruthy();
    expect(container.querySelector(".offline-banner.tone-danger")).toBeTruthy();
  });

  it("断网且冻结：危险提示（欠费派生不可用）", () => {
    const { getByText, container } = render(
      <OfflineBanner
        {...makeStores({ online: false }, { ...READY, status: "frozen", balance: 0, graceDeadlineMs: null })}
      />,
    );
    expect(getByText(/已断网：账号欠费已冻结/)).toBeTruthy();
    expect(container.querySelector(".offline-banner.tone-danger")).toBeTruthy();
  });

  it("断网且无快照（未同步）：提示未同步，不展示「余额 0 分」假象", () => {
    const { getByText, queryByText } = render(
      <OfflineBanner
        {...makeStores({ online: false }, { status: "empty", balance: 0, graceDeadlineMs: null, stale: false })}
      />,
    );
    expect(getByText(/暂无本地权益快照/)).toBeTruthy();
    expect(queryByText(/快照余额/)).toBeNull();
  });
});