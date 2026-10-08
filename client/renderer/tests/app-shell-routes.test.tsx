/**
 * 应用壳路由走查测试（SP3-4）：登录门禁 + 8 页面路由逐一渲染。
 * fake bridge 注入页面数据；页面标题出现即路由与渲染链路打通。
 */

import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { fakeBridge } from "./helpers.ts";
import { App, createStores } from "../src/entry.tsx";
import { BRIDGE_CHANNELS } from "../src/bridges/bridge.ts";
import type { AppStores } from "../src/state/app-stores.ts";

afterEach(cleanup);

/** 登录态基础通道数据（导出用例需持有结果对象引用以便中途改桩）。 */
function baseResults(): Record<string, unknown> {
  return {
    [BRIDGE_CHANNELS.keysList]: [],
    [BRIDGE_CHANNELS.billingOverview]: { planName: "免费版", subEndAt: null, pointsBalance: 0 },
    [BRIDGE_CHANNELS.billingLedger]: [],
    [BRIDGE_CHANNELS.contentList]: [],
    [BRIDGE_CHANNELS.historyList]: [],
    [BRIDGE_CHANNELS.entitlementStatus]: { status: "ready", balance: 93, graceDeadlineMs: Date.now() + 72 * 3600 * 1000 },
  };
}

function signedInStores(extraResults: Record<string, unknown> = {}): AppStores {
  const bridge = fakeBridge({ results: { ...baseResults(), ...extraResults } });
  const stores = createStores(bridge);
  act(() => {
    stores.session.setState({ status: "signed-in", username: "demo" });
  });
  return stores;
}

function goto(path: string): void {
  act(() => {
    window.location.hash = path;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

describe("AppShell 路由走查", () => {
  it("未登录：任意路由回登录页", () => {
    const bridge = fakeBridge();
    render(<App stores={createStores(bridge)} />);
    expect(screen.getByRole("button", { name: "登录" })).toBeTruthy();
  });

  it("登录后 8 页面路由逐一渲染", () => {
    render(<App stores={signedInStores()} />);
    const cases: Array<{ path: string; title: string }> = [
      { path: "/workspace", title: "工作台" },
      { path: "/keys", title: "Key 管理" },
      { path: "/billing", title: "账单" },
      { path: "/content", title: "内容库" },
      { path: "/history", title: "历史" },
      { path: "/compliance", title: "合规导出" },
      { path: "/settings", title: "设置" },
    ];
    for (const item of cases) {
      goto(item.path);
      expect(screen.getAllByRole("heading", { level: 2 })[0].textContent).toContain(item.title);
    }
  });

  it("工作台空态展示示例题引导；一键开始后进入进度视图", async () => {
    const stores = signedInStores();
    render(<App stores={stores} />);
    goto("/workspace");
    expect(screen.getByText(/从示例题开始/)).toBeTruthy();
    act(() => {
      screen.getByRole("button", { name: "一键开始四阶段" }).click();
    });
    // 真实事件流由主进程推送；测试直接注入首条进度（渲染层不关心来源）
    act(() => {
      stores.engine.setState({ taskId: "demo-task", running: true, stage: "analysis" });
    });
    await screen.findByText(/门禁评分规则/);
  });

  it("数据页断网态：连通性离线 + 加载失败 → OfflineState", async () => {
    const bridge = fakeBridge({
      failChannels: new Set([BRIDGE_CHANNELS.keysList]),
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
      stores.connectivity.setState({ online: false });
    });
    render(<App stores={stores} />);
    goto("/keys");
    await screen.findByText(/断网模式/);
  });

  it("数据页空态：空列表 → EmptyState", async () => {
    render(<App stores={signedInStores()} />);
    goto("/history");
    await screen.findByText(/还没有任务/);
  });

  it("账单导出：保存成功 → 展示保存路径；取消 → 清空上次成功文案且不报错", async () => {
    // 同一 results 引用：二次导出前改为「取消」桩，验证旧成功文案被清空（非全新渲染的假阳性）
    const results: Record<string, unknown> = {
      ...baseResults(),
      [BRIDGE_CHANNELS.billingExport]: {
        filename: "erdos-ledger-20261008-1530.csv",
        savedPath: "C:\\Users\\demo\\Downloads\\erdos-ledger-20261008-1530.csv",
        canceled: false,
      },
    };
    const bridge = fakeBridge({ results });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    goto("/billing");
    const button = await screen.findByRole("button", { name: "导出流水（CSV）" });
    await act(async () => {
      button.click();
    });
    expect(screen.getByText(/已保存至 .*erdos-ledger-20261008-1530\.csv/)).toBeTruthy();

    results[BRIDGE_CHANNELS.billingExport] = { filename: "erdos-ledger-x.csv", savedPath: null, canceled: true };
    await act(async () => {
      button.click();
    });
    expect(screen.queryByText(/已保存至|已生成/)).toBeNull();
    expect(document.querySelector(".form-error")).toBeNull();
  });

  it("账单导出：桥报错 → 展示可读错误且不冒充成功（fail-closed）", async () => {
    const bridge = fakeBridge({
      results: baseResults(),
      failChannels: new Set([BRIDGE_CHANNELS.billingExport]),
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    goto("/billing");
    const button = await screen.findByRole("button", { name: "导出流水（CSV）" });
    await act(async () => {
      button.click();
    });
    expect(await screen.findByText(/模拟网络错误/)).toBeTruthy();
    expect(screen.queryByText(/已保存至|已生成/)).toBeNull();
  });
});