/**
 * 启动会话恢复测试（SP3-4 本地安全存储）：
 * 主进程有持久化会话 → 免登录进入（用户名回填）；无会话/异常 → 保持登录页（不阻塞启动）；
 * createStores 入口自动触发恢复。
 */

import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { fakeBridge } from "./helpers.ts";
import { App, createStores } from "../src/entry.tsx";
import { createAppStores, refreshEntitlementAction, restoreSessionAction } from "../src/state/app-stores.ts";
import { BRIDGE_CHANNELS } from "../src/bridges/bridge.ts";

afterEach(cleanup);

/** 恢复进入工作台所需的通道数据（避免未登记通道返回 undefined 干扰渲染）。 */
const SIGNED_IN_RESULTS: Record<string, unknown> = {
  [BRIDGE_CHANNELS.keysList]: [],
  [BRIDGE_CHANNELS.contentList]: [],
  [BRIDGE_CHANNELS.historyList]: [],
  [BRIDGE_CHANNELS.billingOverview]: { planName: "免费版", subEndAt: null, pointsBalance: 100 },
  [BRIDGE_CHANNELS.billingLedger]: [],
  [BRIDGE_CHANNELS.entitlementStatus]: { status: "ready", balance: 100, graceDeadlineMs: null },
};

describe("启动会话恢复", () => {
  it("主进程返回会话视图 → 免登录进入（用户名回填、登录页消失）", async () => {
    const bridge = fakeBridge({
      results: {
        ...SIGNED_IN_RESULTS,
        [BRIDGE_CHANNELS.authSession]: { username: "alice@example.com", expiresInMs: 840_000 },
      },
    });
    const stores = createAppStores(bridge);
    await act(async () => {
      await restoreSessionAction(stores);
    });
    expect(stores.session.getState().status).toBe("signed-in");
    expect(stores.session.getState().username).toBe("alice@example.com");

    render(<App stores={stores} />);
    expect(screen.queryByRole("button", { name: "登录" })).toBeNull();
    expect(screen.getAllByRole("heading", { level: 2 })[0]?.textContent).toContain("工作台");
  });

  it("无会话（null）→ 保持匿名", async () => {
    const bridge = fakeBridge({ results: { [BRIDGE_CHANNELS.authSession]: null } });
    const stores = createAppStores(bridge);
    await act(async () => {
      await restoreSessionAction(stores);
    });
    expect(stores.session.getState().status).toBe("anonymous");
    expect(stores.session.getState().username).toBeNull();
  });

  it("恢复失败（通道异常，如未配置云端）→ 保持匿名且不抛错", async () => {
    const bridge = fakeBridge({ failChannels: new Set([BRIDGE_CHANNELS.authSession]) });
    const stores = createAppStores(bridge);
    await act(async () => {
      await restoreSessionAction(stores);
    });
    expect(stores.session.getState().status).toBe("anonymous");
  });

  it("竞态守卫：恢复响应迟到时不覆盖用户刚完成的登录", async () => {
    let resolveSession: ((value: unknown) => void) | null = null;
    const bridge = {
      invoke: <T = unknown>(channel: string): Promise<T> => {
        if (channel === BRIDGE_CHANNELS.authSession) {
          return new Promise((resolve) => {
            resolveSession = resolve as (value: unknown) => void;
          }) as Promise<T>;
        }
        return Promise.resolve(undefined) as Promise<T>;
      },
      subscribe: () => () => {},
    };
    const stores = createAppStores(bridge);
    const pending = restoreSessionAction(stores);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "new-login@example.com", error: null });
    });
    act(() => {
      resolveSession?.({ username: "stale@example.com", expiresInMs: 1000 });
    });
    await pending;
    expect(stores.session.getState().username).toBe("new-login@example.com"); // 迟到恢复不覆盖
  });

  it("createStores（入口装配）自动触发恢复", async () => {
    const bridge = fakeBridge({
      results: {
        ...SIGNED_IN_RESULTS,
        [BRIDGE_CHANNELS.authSession]: { username: "restored@example.com", expiresInMs: 60_000 },
      },
    });
    const stores = createStores(bridge);
    await act(async () => {}); // flush 恢复的微任务
    expect(stores.session.getState().status).toBe("signed-in");
    expect(stores.session.getState().username).toBe("restored@example.com");
  });
});

describe("启动权益快照回退（SP3-4 第二批）", () => {
  it("主进程回退本地快照（stale=true）→ 状态保留快照余额与宽限倒计时", async () => {
    const deadline = Date.now() + 60_000;
    const bridge = fakeBridge({
      results: {
        [BRIDGE_CHANNELS.entitlementStatus]: {
          status: "ready",
          balance: 100,
          graceDeadlineMs: deadline,
          stale: true,
        },
      },
    });
    const stores = createAppStores(bridge);
    await act(async () => {
      await refreshEntitlementAction(stores);
    });
    expect(stores.entitlement.getState()).toEqual({
      status: "ready",
      balance: 100,
      graceDeadlineMs: deadline,
      stale: true,
    });
  });

  it("通道异常（无本地快照）→ 保留上次状态并标 stale（不清零误导）", async () => {
    const bridge = fakeBridge({ failChannels: new Set([BRIDGE_CHANNELS.entitlementStatus]) });
    const stores = createAppStores(bridge);
    stores.entitlement.setState({ status: "ready", balance: 100, graceDeadlineMs: null, stale: false });
    await act(async () => {
      await refreshEntitlementAction(stores);
    });
    expect(stores.entitlement.getState().balance).toBe(100);
    expect(stores.entitlement.getState().stale).toBe(true);
  });
});