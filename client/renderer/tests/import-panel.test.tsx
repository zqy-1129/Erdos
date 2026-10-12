import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ProblemImportPanel } from "../src/components/problem-import-panel.tsx";
import { fakeBridge } from "./helpers.ts";
import { BRIDGE_CHANNELS as C } from "../src/bridges/bridge.ts";
import type { ImportedProblem } from "../../shared/problem-import.ts";
afterEach(() => { cleanup(); vi.useRealTimers(); });
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (value: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
describe("题面导入进度与取消界面", () => {
  it("显示实际进度、按同一编号取消，结束后可重新导入且停止轮询", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const result = deferred<ImportedProblem | null>(); const calls: Array<{ channel: string; payload: any }> = [];
    const bridge = fakeBridge(); const imported = vi.fn(); const busy = vi.fn();
    bridge.invoke = async <T,>(channel: string, payload?: any): Promise<T> => {
      calls.push({ channel, payload });
      if (channel === C.taskImport) return result.promise as Promise<T>;
      if (channel === C.taskImportStatus) return { importId: payload.importId, phase: "extracting", progress: .42 } as T;
      result.resolve(null); return { cancelled: true } as T;
    };
    render(<ProblemImportPanel bridge={bridge} disabled={false} onImported={imported} onBusyChange={busy} />);
    fireEvent.click(screen.getByText("导入题面文件")); expect(busy).toHaveBeenCalledWith(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(screen.getByRole("progressbar").getAttribute("value")).toBe("42"); expect(screen.getByText(/42%/)).toBeTruthy();
    fireEvent.click(screen.getByText("取消导入")); await screen.findByText("题面导入已取消");
    const start = calls.find(c => c.channel === C.taskImport)!; expect(calls.find(c => c.channel === C.taskImportCancel)?.payload).toEqual(start.payload);
    expect(imported).not.toHaveBeenCalled(); expect(busy).toHaveBeenLastCalledWith(false);
    const count = calls.length; await act(async () => { await vi.advanceTimersByTimeAsync(1000); }); expect(calls.length).toBe(count);
  });
  it("卸载取消本操作并丢弃迟到结果，不改其他页面题面", async () => {
    const result = deferred<ImportedProblem | null>(); const bridge = fakeBridge({ results: { [C.taskImport]: result.promise } });
    const imported = vi.fn(); const busy = vi.fn(); const cancel = vi.spyOn(bridge, "invoke");
    const { unmount } = render(<ProblemImportPanel bridge={bridge} disabled={false} onImported={imported} onBusyChange={busy} />);
    fireEvent.click(screen.getByText("导入题面文件")); unmount();
    expect(cancel.mock.calls.some(([channel]) => channel === C.taskImportCancel)).toBe(true);
    await act(async () => { result.resolve({ title: "old", problemText: "old", sourceName: "old" }); });
    expect(imported).not.toHaveBeenCalled();
  });
  it("进度临时不可达不冒充失败；处理超时给出错误并可重试成功", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const result = deferred<ImportedProblem | null>(); const failures = new Set([C.taskImportStatus]);
    const results: Record<string, unknown> = { [C.taskImport]: result.promise }; const bridge = fakeBridge({ results, failChannels: failures });
    const imported = vi.fn(); render(<ProblemImportPanel bridge={bridge} disabled={false} onImported={imported} onBusyChange={() => {}} />);
    fireEvent.click(screen.getByText("导入题面文件")); await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => { result.reject(new Error("题面解析超时，请拆分文件或重试")); }); expect(screen.getByRole("alert").textContent).toContain("超时");
    results[C.taskImport] = { title: "fresh", problemText: "new", sourceName: "fixture" };
    fireEvent.click(screen.getByText("导入题面文件")); await waitFor(() => expect(imported).toHaveBeenCalledWith(results[C.taskImport]));
    expect(screen.getByText("题面导入完成")).toBeTruthy(); expect(screen.queryByRole("progressbar")).toBeNull();
  });
  it("取消请求失败允许再试；其他操作禁用导入时不发起后台调用", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const result = deferred<ImportedProblem | null>(); const failures = new Set([C.taskImportCancel]);
    const bridge = fakeBridge({ results: { [C.taskImport]: result.promise }, failChannels: failures });
    const old = bridge.invoke; bridge.invoke = async <T,>(channel: string, payload?: any): Promise<T> => channel === C.taskImportStatus ?
      { importId: payload.importId, phase: "reading", progress: 0 } as T : old<T>(channel, payload);
    const { rerender } = render(<ProblemImportPanel bridge={bridge} disabled onImported={() => {}} onBusyChange={() => {}} />);
    expect((screen.getByText("导入题面文件") as HTMLButtonElement).disabled).toBe(true);
    rerender(<ProblemImportPanel bridge={bridge} disabled={false} onImported={() => {}} onBusyChange={() => {}} />);
    fireEvent.click(screen.getByText("导入题面文件")); await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    fireEvent.click(screen.getByText("取消导入")); expect(await screen.findByRole("alert")).toBeTruthy();
    await act(async () => { result.resolve(null); }); expect(screen.getByText("题面导入已取消")).toBeTruthy();
  });
});
