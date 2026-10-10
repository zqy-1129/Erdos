import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { fakeBridge, type FakeBridgeCall } from "./helpers.ts";
import { BRIDGE_CHANNELS as C } from "../src/bridges/bridge.ts";
import { CheckoutPanel } from "../src/components/checkout-panel.tsx";
import { PasswordResetPanel } from "../src/components/password-reset-panel.tsx";
import { UpdatePanel } from "../src/components/update-panel.tsx";
import { ArtifactPanel } from "../src/components/artifact-panel.tsx";
import { WorkspacePage } from "../src/pages/workspace.tsx";
import { SettingsPage } from "../src/pages/settings.tsx";
import { createStores } from "../src/entry.tsx";
import type { GetStatusResult } from "../../shared/ipc.ts";
afterEach(() => { cleanup(); vi.useRealTimers(); });
const product = { id: "p", code: "pack", name: "积分包", type: "points_pack", price_cents: 199, points: 50, duration_days: 0 };
const order = { id: "o", product_id: "p", price_cents: 199, channel: "alipay", status: "created", idempotency_key: "r", created_at: "2026-10-10T10:00:00Z", expires_at: "2026-10-10T10:30:00Z" };
describe("购买与密码找回界面", () => {
  it("下单、30秒轮询、到账刷新和卸载停止轮询", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const calls: FakeBridgeCall[] = []; const paid = vi.fn();
    const results: Record<string, unknown> = { [C.billingProducts]: [product], [C.billingPendingOrder]: null, [C.billingCreateOrder]: { order, product, already_exists: false } };
    const { unmount } = render(<CheckoutPanel bridge={fakeBridge({ calls, results })} onPaid={paid} />);
    await screen.findByText(/积分包 · ¥1.99/);
    fireEvent.click(screen.getByRole("button", { name: /积分包/ })); await screen.findByText(/等待付款/);
    expect(calls.find(c => c.channel === C.billingCreateOrder)?.payload).toEqual({ productCode: "pack", channel: "alipay" });
    results[C.billingPendingOrder] = { ...order, status: "paid" };
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByText(/已到账/)).toBeTruthy(); expect(paid).toHaveBeenCalledTimes(1);
    unmount(); const count = calls.length; await act(async () => { await vi.advanceTimersByTimeAsync(60000); }); expect(calls.length).toBe(count);
  });
  it("商品加载/购买失败有重试；密码令牌和密码重置后清空", async () => {
    const calls: FakeBridgeCall[] = []; const failures = new Set([C.billingCreateOrder]);
    render(<CheckoutPanel bridge={fakeBridge({ calls, results: { [C.billingProducts]: [product], [C.billingPendingOrder]: null }, failChannels: failures })} onPaid={() => {}} />);
    await screen.findByText(/积分包/); fireEvent.change(screen.getByLabelText("支付方式"), { target: { value: "wechat" } });
    fireEvent.click(screen.getByRole("button", { name: /积分包/ })); await screen.findByRole("alert");
    cleanup();
    const bridge = fakeBridge({ calls, results: { [C.authResetRequest]: { accepted: true }, [C.authResetConfirm]: { accepted: true } } });
    render(<PasswordResetPanel bridge={bridge} />);
    fireEvent.click(screen.getByText("忘记密码"));
    fireEvent.change(screen.getByLabelText("重置账号"), { target: { value: "unit@example.com" } });
    fireEvent.click(screen.getByText("发送重置请求")); await screen.findByText(/请求已受理/);
    fireEvent.change(screen.getByLabelText("重置令牌"), { target: { value: "unit-reset-token-123" } });
    fireEvent.change(screen.getByLabelText("新密码"), { target: { value: "UnitPass123" } });
    fireEvent.click(screen.getByText("确认重置密码")); await screen.findByText(/密码已重置/);
    expect((screen.getByLabelText("重置令牌") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("新密码") as HTMLInputElement).value).toBe("");
  });
});
describe("产物、设置与更新", () => {
  it("文本预览和导出只传索引号，切换任务清空预览", async () => {
    const calls: FakeBridgeCall[] = []; const results = { [C.artifactsList]: [{ id: "sha.0", name: "paper.md", stage: "writing", size: 100 }],
      [C.artifactsPreview]: { kind: "text", text: "unit-paper", name: "paper.md" }, [C.artifactsSave]: { canceled: false } };
    render(<ArtifactPanel taskId="t" revision={1} bridge={fakeBridge({ calls, results })} />);
    await screen.findByText(/paper.md/); fireEvent.click(screen.getByText("预览")); await screen.findByText("unit-paper");
    fireEvent.click(screen.getByText("导出")); await waitFor(() => expect(calls.some(c => c.channel === C.artifactsSave)).toBe(true));
    expect(calls.find(c => c.channel === C.artifactsPreview)?.payload).toEqual({ taskId: "t", id: "sha.0" });
  });
  it("真实偏好保存失败不显示成功；更新由用户检查、下载和安装", async () => {
    const results: Record<string, unknown> = { [C.preferencesGet]: { defaultModel: "unit", language: "zh-CN" }, [C.updateStatus]: { status: "disabled", currentVersion: "0.1.0", message: "开发版本" } };
    const failures = new Set([C.preferencesSave]); const bridge = fakeBridge({ results, failChannels: failures });
    render(<SettingsPage stores={createStores(bridge)} />);
    await waitFor(() => expect((screen.getByLabelText("默认模型") as HTMLInputElement).value).toBe("unit"));
    fireEvent.click(screen.getByText("保存偏好")); await screen.findByRole("alert"); expect(screen.queryByText("已保存")).toBeNull();
    failures.clear(); fireEvent.change(screen.getByLabelText("默认模型"), { target: { value: "other" } }); fireEvent.click(screen.getByText("保存偏好")); await screen.findByText("已保存");
    cleanup();
    const calls: FakeBridgeCall[] = []; results[C.updateStatus] = { status: "idle", currentVersion: "0.1.0" };
    results[C.updateCheck] = { status: "available", currentVersion: "0.1.0", version: "0.2.0" };
    results[C.updateDownload] = { status: "downloaded", currentVersion: "0.1.0", version: "0.2.0" };
    results[C.updateInstall] = { status: "downloaded", currentVersion: "0.1.0" };
    render(<UpdatePanel bridge={fakeBridge({ results, calls })} />);
    await screen.findByText(/当前版本/); fireEvent.click(screen.getByText("检查更新")); await screen.findByText("下载更新");
    fireEvent.click(screen.getByText("下载更新")); await screen.findByText("安装并重启"); fireEvent.click(screen.getByText("安装并重启"));
    await waitFor(() => expect(calls.some(c => c.channel === C.updateInstall)).toBe(true));
  });
});
describe("工作台真实输入和阶段控制", () => {
  it("重新进入工作台从主进程恢复完成状态，隐藏门禁并允许新建", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const stores = createStores(fakeBridge({ results: {
      [C.artifactsList]: [], [C.taskStatus]: { taskId: "t", completed: true },
      "engine:get_status": { engine: "idle", task: { task_id: "t", stage: "writing", status: "done" } },
    } }));
    stores.engine.setState({ taskId: "t", stage: "writing", running: false });
    render(<WorkspacePage stores={stores} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText(/四阶段已完成/)).toBeTruthy();
    expect(screen.queryByText(/阶段待审批/)).toBeNull();
    expect(screen.getByText("新建任务")).toBeTruthy();
  });
  it("导入题面、登记后启动、门禁通过完成；取消、暂停恢复调用", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let status: GetStatusResult = { engine: "idle", task: null }; const calls: FakeBridgeCall[] = [];
    const bridge = fakeBridge({ results: { [C.artifactsList]: [] } });
    const base = bridge.invoke;
    bridge.invoke = async <T,>(channel: string, payload?: any): Promise<T> => {
      calls.push({ channel, payload });
      if (channel === C.taskImport) return { title: "真实题面", problemText: "unit problem" } as T;
      if (channel === "engine:get_status") return status as T;
      if (channel === "engine:start_stage") status = { engine: "running", task: { task_id: payload.task_id, stage: payload.stage, status: "running" } };
      if (channel === "engine:pause") status.task!.status = "paused";
      if (channel === "engine:resume") status.task!.status = "running";
      if (channel === "engine:cancel") status.task!.status = "cancelled";
      if (channel === "engine:answer_gate") return { action: "complete" } as T;
      return base<T>(channel, payload);
    };
    const stores = createStores(bridge); render(<WorkspacePage stores={stores} />);
    fireEvent.click(screen.getByText("开始分析")); await screen.findByText("请填写任务标题和题面");
    fireEvent.click(screen.getByText("导入题面文件")); await waitFor(() => expect((screen.getByLabelText("任务标题") as HTMLInputElement).value).toBe("真实题面"));
    fireEvent.click(screen.getByText("开始分析")); await screen.findByText(/当前阶段配额/);
    const create = calls.find(c => c.channel === "engine:task_create")!;
    expect((create.payload as { problem_text: string }).problem_text).toBe("unit problem");
    fireEvent.click(screen.getByText("暂停")); await waitFor(() => expect((screen.getByText("继续") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByText("继续")); await waitFor(() => expect(stores.engine.getState().running).toBe(true));
    status.task!.stage = "writing"; status.task!.status = "done";
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText(/阶段待审批/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /通过/ }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText(/四阶段已完成/)).toBeTruthy();
    fireEvent.click(screen.getByText("新建任务")); expect(screen.getByText("开始分析")).toBeTruthy();
  });
});
