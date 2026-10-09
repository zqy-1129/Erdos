/**
 * 应用壳路由走查测试（SP3-4）：登录门禁 + 8 页面路由逐一渲染。
 * fake bridge 注入页面数据；页面标题出现即路由与渲染链路打通。
 */

import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { fakeBridge, type FakeBridgeCall } from "./helpers.ts";
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
    [BRIDGE_CHANNELS.trailRecentTasks]: [],
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

  it("合规导出：无任务本地拦截（不调用通道）；有任务时携带任务号并渲染留痕预览", async () => {
    const calls: FakeBridgeCall[] = [];
    const bridge = fakeBridge({
      results: {
        ...baseResults(),
        [BRIDGE_CHANNELS.complianceExport]: {
          content: "AI 工具使用声明（真实留痕）",
          filename: "AI工具使用声明_t-1.md",
          artifactHashes: ["a".repeat(64)],
        },
      },
      calls,
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    goto("/compliance");
    const note = await screen.findByRole("textbox");
    fireEvent.change(note, { target: { value: "数据预处理由本人手动完成。" } });

    // 无任务：声明依据任务留痕生成，页面先行拦截（不发起桥调用）
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    expect(screen.getByText(/暂无任务：请先/)).toBeTruthy();
    expect(calls.some((call) => call.channel === BRIDGE_CHANNELS.complianceExport)).toBe(false);

    // 有任务（引擎事件已归约出 taskId）：携带任务号调用并渲染预览
    act(() => {
      stores.engine.setState({ taskId: "t-1" });
    });
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    const exportCall = calls.find((call) => call.channel === BRIDGE_CHANNELS.complianceExport);
    expect(exportCall?.payload).toMatchObject({ taskId: "t-1", format: "md", unusedAi: false });
    expect(await screen.findByText(/AI工具使用声明_t-1\.md/)).toBeTruthy();
    expect(screen.getByText(/AI 工具使用声明（真实留痕）/)).toBeTruthy();
  });

  it("合规导出：桥失败清空旧预览并展示可读错误；未使用 AI 页脚与哈希口径一致", async () => {
    const failChannels = new Set<string>();
    const bridge = fakeBridge({
      results: {
        ...baseResults(),
        [BRIDGE_CHANNELS.complianceExport]: {
          content: "预览正文（旧）",
          filename: "AI工具使用声明_t-2.md",
          artifactHashes: [],
        },
      },
      failChannels,
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
      stores.engine.setState({ taskId: "t-2" });
    });
    render(<App stores={stores} />);
    goto("/compliance");
    const note = await screen.findByRole("textbox");
    fireEvent.change(note, { target: { value: "说明" } });
    fireEvent.click(screen.getByRole("checkbox")); // 未使用 AI 版本

    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    expect(await screen.findByText(/预览正文（旧）/)).toBeTruthy();
    expect(screen.getByText(/未使用 AI 版本：声明不含工具清单/)).toBeTruthy();
    expect(screen.queryByText(/产物哈希（可信声明依据）/)).toBeNull();

    // 二次导出：桥失败 → 旧预览被清空、展示可读错误（不冒充成功）
    failChannels.add(BRIDGE_CHANNELS.complianceExport);
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    expect(await screen.findByText(/模拟网络错误/)).toBeTruthy();
    expect(screen.queryByText(/预览正文（旧）/)).toBeNull();
  });

  it("合规保存：无任务本地拦截（不调通道）；成功展示保存路径；取消静默清空旧文案；失败可读", async () => {
    // 同一 results 引用：后续步骤改桩验证「旧成功文案被清空」「失败不冒充成功」（非全新渲染的假阳性）
    const calls: FakeBridgeCall[] = [];
    const failChannels = new Set<string>();
    const results: Record<string, unknown> = {
      ...baseResults(),
      [BRIDGE_CHANNELS.complianceSave]: {
        filename: "AI工具使用声明_t-3.docx",
        savedPath: "/home/demo/AI工具使用声明_t-3.docx",
        canceled: false,
      },
    };
    const bridge = fakeBridge({ results, calls, failChannels });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    goto("/compliance");
    const note = await screen.findByRole("textbox");
    fireEvent.change(note, { target: { value: "图由本人核验。" } });

    // 无任务：声明依据任务留痕，页面先行拦截（不发起桥调用）
    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    expect(screen.getByText(/暂无任务：请先/)).toBeTruthy();
    expect(calls.some((call) => call.channel === BRIDGE_CHANNELS.complianceSave)).toBe(false);

    // 有任务：保存成功 → 展示保存路径（格式/说明随页面状态透传）
    act(() => {
      stores.engine.setState({ taskId: "t-3" });
    });
    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    const saveCall = calls.find((call) => call.channel === BRIDGE_CHANNELS.complianceSave);
    expect(saveCall?.payload).toMatchObject({ taskId: "t-3", format: "md", unusedAi: false, humanNote: "图由本人核验。" });
    expect(await screen.findByText(/已保存至 .*AI工具使用声明_t-3\.docx/)).toBeTruthy();

    // 输入变更（说明/格式）：旧「已保存至」清除，避免过期指引误认为当前输入已保存
    fireEvent.change(note, { target: { value: "图由本人核验。v2" } });
    expect(screen.queryByText(/已保存至|已生成/)).toBeNull();

    // 用户取消：先恢复一次成功提示，再验证取消时清空且静默（非失败）
    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    expect(await screen.findByText(/已保存至 .*AI工具使用声明_t-3\.docx/)).toBeTruthy();
    results[BRIDGE_CHANNELS.complianceSave] = { filename: "AI工具使用声明_t-3.md", savedPath: null, canceled: true };
    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    expect(screen.queryByText(/已保存至|已生成/)).toBeNull();
    expect(document.querySelector(".form-error")).toBeNull();

    // 无路径回显（演示/未给路径）：savedPath 为 null 且未取消 → 展示将保存的文件名（不冒充已保存）
    results[BRIDGE_CHANNELS.complianceSave] = { filename: "AI工具使用声明_t-3.md", savedPath: null, canceled: false };
    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    expect(await screen.findByText(/已生成 AI工具使用声明_t-3\.md/)).toBeTruthy();

    // 桥失败：可读错误且不冒充成功（fail-closed）
    failChannels.add(BRIDGE_CHANNELS.complianceSave);
    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    expect(await screen.findByText(/模拟网络错误/)).toBeTruthy();
    expect(screen.queryByText(/已保存至|已生成/)).toBeNull();
  });

  it("合规任务来源：无会话任务时选最近留痕历史任务（重启后导出/保存携带所选任务号）", async () => {
    const calls: FakeBridgeCall[] = [];
    const bridge = fakeBridge({
      results: {
        ...baseResults(),
        [BRIDGE_CHANNELS.trailRecentTasks]: [
          { taskId: "t-9", lastTs: "2026-10-08T02:00:00+00:00", eventCount: 5 },
        ],
        [BRIDGE_CHANNELS.complianceExport]: {
          content: "预览（历史任务 t-9）",
          filename: "AI工具使用声明_t-9.md",
          artifactHashes: [],
        },
        [BRIDGE_CHANNELS.complianceSave]: {
          filename: "AI工具使用声明_t-9.md",
          savedPath: "/home/demo/AI工具使用声明_t-9.md",
          canceled: false,
        },
      },
      calls,
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    goto("/compliance");
    const note = await screen.findByRole("textbox");
    fireEvent.change(note, { target: { value: "历史任务说明。" } });

    // 重启后（无会话任务）：默认未选任务 → 本地拦截（不猜测任务、不发起桥调用）
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    expect(screen.getByText(/暂无任务：请先/)).toBeTruthy();
    expect(calls.some((call) => call.channel === BRIDGE_CHANNELS.complianceExport)).toBe(false);

    // 从「任务来源」选择最近留痕任务 → 导出/保存均携带所选任务号
    const picker = screen.getByRole("combobox", { name: "任务来源" });
    expect(await screen.findByRole("option", { name: /5 条留痕/ })).toBeTruthy();
    fireEvent.change(picker, { target: { value: "t-9" } });
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    const exportCall = calls.find((call) => call.channel === BRIDGE_CHANNELS.complianceExport);
    expect(exportCall?.payload).toMatchObject({ taskId: "t-9", format: "md", unusedAi: false, humanNote: "历史任务说明。" });
    expect(await screen.findByText(/AI工具使用声明_t-9\.md/)).toBeTruthy();

    await act(async () => {
      screen.getByRole("button", { name: "保存文件" }).click();
    });
    const saveCall = calls.find((call) => call.channel === BRIDGE_CHANNELS.complianceSave);
    expect(saveCall?.payload).toMatchObject({ taskId: "t-9", format: "md", humanNote: "历史任务说明。" });
    expect(await screen.findByText(/已保存至 .*AI工具使用声明_t-9\.md/)).toBeTruthy();

    // 任务来源变更：旧预览与保存提示清除（避免「预览的是 A、保存的是 B」与过期提示）
    fireEvent.change(picker, { target: { value: "" } });
    expect(screen.queryByText(/预览（历史任务 t-9）/)).toBeNull();
    expect(screen.queryByText(/已保存至|已生成/)).toBeNull();
  });

  it("合规任务来源：会话任务与历史任务并存时显式选择优先（会话任务不重复列出）", async () => {
    const calls: FakeBridgeCall[] = [];
    const bridge = fakeBridge({
      results: {
        ...baseResults(),
        [BRIDGE_CHANNELS.trailRecentTasks]: [
          { taskId: "t-1", lastTs: "2026-10-08T03:00:00+00:00", eventCount: 9 },
          { taskId: "t-9", lastTs: "2026-10-08T02:00:00+00:00", eventCount: 5 },
        ],
        [BRIDGE_CHANNELS.complianceExport]: {
          content: "预览（任务）",
          filename: "AI工具使用声明_x.md",
          artifactHashes: [],
        },
      },
      calls,
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
      stores.engine.setState({ taskId: "t-1" });
    });
    render(<App stores={stores} />);
    goto("/compliance");
    const picker = screen.getByRole("combobox", { name: "任务来源" });
    expect(await screen.findByRole("option", { name: /t-9（最近活动/ })).toBeTruthy();
    expect(screen.getAllByRole("option", { name: /t-1/ }).length).toBe(1); // 会话任务仅「当前任务」一项（去重）

    // 默认取会话任务 → 导出携带 t-1
    const note = await screen.findByRole("textbox");
    fireEvent.change(note, { target: { value: "说明。" } });
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    expect(calls.find((call) => call.channel === BRIDGE_CHANNELS.complianceExport)?.payload).toMatchObject({
      taskId: "t-1",
    });

    // 显式选择历史任务 → 导出切换为 t-9（显式选择优先于会话任务）
    fireEvent.change(picker, { target: { value: "t-9" } });
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    const exportCalls = calls.filter((call) => call.channel === BRIDGE_CHANNELS.complianceExport);
    expect(exportCalls[1]?.payload).toMatchObject({ taskId: "t-9" });
  });

  it("合规任务来源：最近任务读取失败 → 可读提示且会话任务流程不受阻（非阻断）", async () => {
    const calls: FakeBridgeCall[] = [];
    const bridge = fakeBridge({
      results: {
        ...baseResults(),
        [BRIDGE_CHANNELS.complianceExport]: {
          content: "预览（会话任务）",
          filename: "AI工具使用声明_t-1.md",
          artifactHashes: [],
        },
      },
      failChannels: new Set([BRIDGE_CHANNELS.trailRecentTasks]),
      calls,
    });
    const stores = createStores(bridge);
    act(() => {
      stores.session.setState({ status: "signed-in", username: "demo" });
    });
    render(<App stores={stores} />);
    goto("/compliance");

    // 无会话任务：占位项受失败态约束（不误报「未发现任务」）+ 可读错误提示
    expect(await screen.findByRole("option", { name: "最近任务读取失败（见下方提示）" })).toBeTruthy();
    expect(screen.getByText(/最近任务读取失败：/)).toBeTruthy();

    // 会话任务流程不受列表失败影响
    act(() => {
      stores.engine.setState({ taskId: "t-1" });
    });
    const note = await screen.findByRole("textbox");
    fireEvent.change(note, { target: { value: "会话任务说明。" } });
    await act(async () => {
      screen.getByRole("button", { name: "生成声明" }).click();
    });
    const exportCall = calls.find((call) => call.channel === BRIDGE_CHANNELS.complianceExport);
    expect(exportCall?.payload).toMatchObject({ taskId: "t-1" });
    expect(await screen.findByText(/AI工具使用声明_t-1\.md/)).toBeTruthy();
  });
});