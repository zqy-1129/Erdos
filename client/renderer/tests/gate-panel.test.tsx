/**
 * 门禁审批面板组件测试（FE-APPROVE W13）：三态 + 校验 + 冲突 + 重试计数。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { GatePanel } from "../src/components/gate-panel.tsx";

afterEach(cleanup);

describe("GatePanel 门禁审批（FE-APPROVE W13）", () => {
  it("展示 reason 全文与重试计数 x/3", () => {
    render(<GatePanel gate="建模" reason="目标函数未包含变量约束" retries={2} onSubmit={async () => {}} />);
    expect(screen.getByText(/目标函数未包含变量约束/)).toBeTruthy();
    expect(screen.getByText(/重试 2\/3/)).toBeTruthy();
  });

  it("reject 未填 feedback → 校验错误且不提交", async () => {
    const onSubmit = vi.fn(async () => {});
    render(<GatePanel gate="建模" reason="r" retries={1} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("拒绝并反馈"));
    expect(await screen.findByText(/拒绝时必须填写修改意见/)).toBeTruthy();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("pass 提交：onSubmit(pass, '')", async () => {
    const onSubmit = vi.fn(async () => {});
    render(<GatePanel gate="建模" reason="r" retries={1} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("通过"));
    expect(onSubmit).toHaveBeenCalledWith("pass", "");
  });

  it("reject 填 feedback 提交：onSubmit(reject, feedback)", async () => {
    const onSubmit = vi.fn(async () => {});
    render(<GatePanel gate="建模" reason="r" retries={1} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByPlaceholderText(/说明问题所在/), { target: { value: "补充变量约束" } });
    fireEvent.click(screen.getByText("拒绝并反馈"));
    expect(onSubmit).toHaveBeenCalledWith("reject", "补充变量约束");
  });

  it("提交中 busy 禁用按钮", async () => {
    let resolve!: () => void;
    const onSubmit = vi.fn(() => new Promise<void>((r) => (resolve = r)));
    render(<GatePanel gate="建模" reason="r" retries={1} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("通过"));
    expect(screen.getByText("通过").closest("button")?.disabled).toBe(true);
    resolve();
    await act(async () => {});
  });

  it("onSubmit 抛错 → 显示冲突错误且保留 feedback 草稿", async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error("门禁冲突：任务已流转");
    });
    render(<GatePanel gate="建模" reason="r" retries={1} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByPlaceholderText(/说明问题所在/), { target: { value: "草稿意见" } });
    fireEvent.click(screen.getByText("拒绝并反馈"));
    expect(await screen.findByText(/门禁冲突/)).toBeTruthy();
    expect((screen.getByPlaceholderText(/说明问题所在/) as HTMLTextAreaElement).value).toBe("草稿意见");
  });
});
