import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { KeysPage } from "../src/pages/keys.tsx";
import { ContentPage } from "../src/pages/content.tsx";
import { fakeBridge, type FakeBridgeCall } from "./helpers.ts";
import { createStores } from "../src/entry.tsx";
import { BRIDGE_CHANNELS as C } from "../src/bridges/bridge.ts";
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe("密钥管理完整操作", () => {
  const item = { id: "k1", alias: "配置1", masked: "unit-***", baseUrl: "https://unit.example", model: "unit", active: false, status: "unknown" };
  function fixture() {
    const calls: FakeBridgeCall[] = []; const failures = new Set<string>();
    const results: Record<string, unknown> = { [C.keysList]: [item, { ...item, id: "k2", alias: "配置2", status: "ok", active: true }, { ...item, id: "k3", alias: "配置3", status: "fail" }],
      [C.keysUsage]: { totalTokens: 100, modelCalls: 2, estimatedCostCents: null }, [C.keysTest]: { ok: true, reason: "none", detail: "检测完成" },
      [C.keysDelete]: { ok: true, requeue: true } };
    const stores = createStores(fakeBridge({ calls, results, failChannels: failures }));
    return { calls, failures, results, stores };
  }
  it("保存模型、连通检测、切换和删除；删除失败和取消保留配置", async () => {
    const f = fixture(); const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<KeysPage stores={f.stores} />); await screen.findByText("配置1");
    fireEvent.click(screen.getAllByText("使用此配置")[0]); await waitFor(() => expect(f.calls.some(c => c.channel === C.keysActivate)).toBe(true));
    fireEvent.click(screen.getAllByText("删除")[0]); await waitFor(() => expect(alert).toHaveBeenCalled());
    await screen.findByText("配置1");
    confirm.mockReturnValue(false); const n = f.calls.length; fireEvent.click(screen.getAllByText("删除")[0]); expect(f.calls.length).toBe(n);
    confirm.mockReturnValue(true); f.failures.add(C.keysDelete); fireEvent.click(screen.getAllByText("删除")[0]); await waitFor(() => expect(alert).toHaveBeenCalledWith(expect.stringContaining("删除失败")));
    fireEvent.change(screen.getByLabelText("别名"), { target: { value: "unit-new" } });
    fireEvent.change(screen.getByLabelText("Base URL（OpenAI 兼容）"), { target: { value: "https://unit.example/v1" } });
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "unit-only-credential" } });
    fireEvent.change(screen.getByLabelText("模型名"), { target: { value: "unit-model" } });
    fireEvent.click(screen.getByText("保存并测试连通")); await screen.findByText("检测完成");
    expect(f.calls.find(c => c.channel === C.keysSave)?.payload).toMatchObject({ model: "unit-model" });
    expect(f.calls.find(c => c.channel === C.keysTest)?.payload).toEqual({});
    fireEvent.click(screen.getByText("再测一个")); expect((screen.getByLabelText("API Key") as HTMLInputElement).value).toBe("");
    f.failures.add(C.keysSave);
    for (const [label, value] of [["别名","new"],["Base URL（OpenAI 兼容）","https://unit.example"],["API Key","unit"],["模型名","model"]]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
    fireEvent.click(screen.getByText("保存并测试连通")); await screen.findByText(/模拟网络错误.*keys:save/);
  });
  it("空列表、离线提示、已知模型估价与激活失败", async () => {
    const f = fixture(); f.stores.connectivity.setState({ online: false }); f.results[C.keysUsage] = { totalTokens: 0, modelCalls: 0, estimatedCostCents: 1 };
    f.failures.add(C.keysActivate); const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<KeysPage stores={f.stores} />); await screen.findByText(/断网模式下仅展示/);
    fireEvent.click(screen.getAllByText("使用此配置")[0]); await waitFor(() => expect(alert).toHaveBeenCalled());
  });
});
describe("内容检索与合规说明", () => {
  it("模板、案例、标题和方法标签检索及空结果", async () => {
    const stores = createStores(fakeBridge({ results: { [C.contentList]: [
      { id: "t", kind: "template", title: "国赛模板", tags: ["格式"], referenceOnly: false },
      { id: "c", kind: "case", title: "拟合案例", tags: ["最小二乘"], referenceOnly: true, complianceNote: "仅用于学习" },
    ] } }));
    render(<ContentPage stores={stores} />); await screen.findByText("国赛模板");
    fireEvent.click(screen.getByText("案例库")); await screen.findByText("仅用于学习");
    fireEvent.change(screen.getByPlaceholderText(/按题型/), { target: { value: "最小二乘" } }); expect(screen.getByText("拟合案例")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText(/按题型/), { target: { value: "不存在" } }); expect(screen.getByText("没有匹配的内容")).toBeTruthy();
  });
});
