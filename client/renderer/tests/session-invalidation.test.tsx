/**
 * 会话失效下发测试（上批评审遗留项修复）：
 * 主进程业务 401 清会话后推送 auth:session-invalidated → 渲染层回登录页并显示可读提示；
 * dispose 后订阅清理，事件不再改变会话。
 */

import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { fakeBridgeHandle } from "./helpers.ts";
import { App, createStores } from "../src/entry.tsx";
import { BRIDGE_CHANNELS } from "../src/bridges/bridge.ts";

afterEach(cleanup);

/** 登录态进入工作台所需的通道数据（避免未登记通道返回 undefined 干扰渲染）。 */
const SIGNED_IN_RESULTS: Record<string, unknown> = {
  [BRIDGE_CHANNELS.keysList]: [],
  [BRIDGE_CHANNELS.contentList]: [],
  [BRIDGE_CHANNELS.historyList]: [],
  [BRIDGE_CHANNELS.billingOverview]: { planName: "免费版", subEndAt: null, pointsBalance: 100 },
  [BRIDGE_CHANNELS.billingLedger]: [],
  [BRIDGE_CHANNELS.entitlementStatus]: { status: "ready", balance: 100, graceDeadlineMs: null },
};

describe("会话失效下发", () => {
  it("登录态收到 auth:session-invalidated → 回登录页并提示重新登录", () => {
    const handle = fakeBridgeHandle({ results: SIGNED_IN_RESULTS });
    const stores = createStores(handle.bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    expect(screen.queryByRole("button", { name: "登录" })).toBeNull(); // 登录态无登录页

    act(() => {
      handle.emit(BRIDGE_CHANNELS.authSessionInvalidated, { reason: "unauthorized" });
    });
    expect(screen.getByRole("button", { name: "登录" })).toBeTruthy();
    expect(screen.getByText(/登录已失效/)).toBeTruthy();
    expect(stores.session.getState().status).toBe("anonymous");
  });

  it("未登录/已登出时收到（迟到）事件 → 忽略，不产生噪声提示", () => {
    const handle = fakeBridgeHandle({ results: SIGNED_IN_RESULTS });
    const stores = createStores(handle.bridge);
    act(() => {
      handle.emit(BRIDGE_CHANNELS.authSessionInvalidated, { reason: "unauthorized" });
    });
    expect(stores.session.getState().status).toBe("anonymous");
    expect(stores.session.getState().error).toBeNull();
  });

  it("并发重复下发只回落一次（第二次不覆盖已展示状态）", () => {
    const handle = fakeBridgeHandle({ results: SIGNED_IN_RESULTS });
    const stores = createStores(handle.bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    act(() => {
      handle.emit(BRIDGE_CHANNELS.authSessionInvalidated, { reason: "unauthorized" }); // 第一次：回落登录页
    });
    expect(stores.session.getState().status).toBe("anonymous");
    act(() => {
      stores.session.setState({ error: "哨兵：后续事件不应覆盖" });
      handle.emit(BRIDGE_CHANNELS.authSessionInvalidated, { reason: "unauthorized" }); // 第二次：被守卫忽略
    });
    expect(stores.session.getState().error).toBe("哨兵：后续事件不应覆盖");
  });

  it("dispose 后订阅清理：事件不再改变会话", () => {
    const handle = fakeBridgeHandle({ results: SIGNED_IN_RESULTS });
    const stores = createStores(handle.bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    stores.dispose();
    act(() => {
      handle.emit(BRIDGE_CHANNELS.authSessionInvalidated, { reason: "unauthorized" });
    });
    expect(stores.session.getState().status).toBe("signed-in");
  });
});