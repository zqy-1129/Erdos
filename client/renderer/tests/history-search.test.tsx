import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HistoryPage } from "../src/pages/history.tsx";
import { createStores } from "../src/entry.tsx";
import { fakeBridge } from "./helpers.ts";
import { BRIDGE_CHANNELS as C } from "../src/bridges/bridge.ts";
import { paginateHistory, parseHistoryQuery, type HistoryTask } from "../../shared/history.ts";
afterEach(() => { cleanup(); vi.useRealTimers(); });
const tasks: HistoryTask[] = Array.from({ length: 65 }, (_, i) => ({ taskId: "t-" + i, title: i === 60 ? "更早的目标题" : "任务" + i,
  status: i % 2 ? "modeling" : "done", resumable: !!(i % 2), updatedAt: "2026-10-12T01:00:00Z" }));

describe("历史搜索、分页与错误恢复", () => {
  it("搜索覆盖较旧任务，快速输入只发最终查询；筛选分页保持正确编号", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const calls: Array<{ channel: string; payload: any }> = []; const bridge = fakeBridge();
    bridge.invoke = async <T,>(channel: string, payload?: any): Promise<T> => {
      calls.push({ channel, payload }); return paginateHistory(tasks, parseHistoryQuery(payload)) as T;
    };
    render(<HistoryPage stores={createStores(bridge)} />); await act(async () => { await Promise.resolve(); });
    expect(screen.getByText(/已加载 50 条/)).toBeTruthy();
    fireEvent.click(screen.getByText("加载更多历史")); await act(async () => { await Promise.resolve(); });
    expect(screen.getByText(/已加载 65 条/)).toBeTruthy(); expect(screen.queryByText("加载更多历史")).toBeNull();
    fireEvent.scroll(screen.getByRole("list"), { target: { scrollTop: 3000 } });
    expect(screen.queryByText("任务0")).toBeNull();
    fireEvent.change(screen.getByLabelText("搜索历史"), { target: { value: "更" } });
    fireEvent.change(screen.getByLabelText("搜索历史"), { target: { value: "更早的" } });
    const previous = calls.length; await act(async () => { await vi.advanceTimersByTimeAsync(249); }); expect(calls.length).toBe(previous);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); }); expect(screen.getByText("更早的目标题")).toBeTruthy();
    expect(calls.at(-1)?.payload).toEqual({ query: "更早的", status: "all", offset: 0, limit: 50 });
    fireEvent.change(screen.getByLabelText("任务状态"), { target: { value: "resumable" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(250); }); expect(screen.getByText("没有匹配的任务")).toBeTruthy();
  });
  it("加载更多失败保留旧记录，重试同一页；刷新重新加载第一页", async () => {
    const bridge = fakeBridge(); let fail = true; const requests: any[] = [];
    bridge.invoke = async <T,>(channel: string, payload?: any): Promise<T> => {
      requests.push(payload); if (payload.offset === 50 && fail) throw new Error("fixture history failed");
      return paginateHistory(tasks, parseHistoryQuery(payload)) as T;
    };
    render(<HistoryPage stores={createStores(bridge)} />); await screen.findByText(/已加载 50 条/);
    fireEvent.click(screen.getByText("加载更多历史")); await screen.findByText("fixture history failed"); expect(screen.getByText("任务0")).toBeTruthy();
    fail = false; fireEvent.click(screen.getByText("重试")); await screen.findByText(/已加载 65 条/);
    expect(requests.at(-1)?.offset).toBe(50); fireEvent.click(screen.getByText("刷新历史")); await screen.findByText(/已加载 50 条/);
    expect(requests.at(-1)?.offset).toBe(0);
  });
  it("迟到查询结果在筛选变化后忽略，不覆盖新列表；响应异常可重试", async () => {
    let resolve!: (value: unknown) => void; const old = new Promise(yes => { resolve = yes; }); let first = true;
    const bridge = fakeBridge(); bridge.invoke = async <T,>(_c: string, payload?: any): Promise<T> => {
      if (first) { first = false; return old as Promise<T>; }
      return paginateHistory(tasks, parseHistoryQuery(payload)) as T;
    };
    render(<HistoryPage stores={createStores(bridge)} />);
    fireEvent.change(screen.getByLabelText("搜索历史"), { target: { value: "更早的" } }); await screen.findByText("更早的目标题");
    await act(async () => { resolve({ items: [{ ...tasks[0], title: "迟到旧结果" }], total: 1, nextOffset: null }); });
    expect(screen.queryByText("迟到旧结果")).toBeNull();
    cleanup(); const result: Record<string, unknown> = { [C.historyQuery]: null };
    render(<HistoryPage stores={createStores(fakeBridge({ results: result }))} />); await screen.findByText("历史记录响应无效，请重试");
    result[C.historyQuery] = { items: [], total: 0, nextOffset: null }; fireEvent.click(screen.getByText("重试")); await waitFor(() => expect(screen.getByText("还没有任务")).toBeTruthy());
  });
});
