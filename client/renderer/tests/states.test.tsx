/**
 * 三态占位组件测试（SP3-4）：空态/异常态/断网态/加载态。
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { EmptyState, ErrorState, LoadingState, OfflineState } from "../src/components/states.tsx";

afterEach(cleanup);

describe("三态组件", () => {
  it("空态：标题/提示/动作渲染", () => {
    render(
      <EmptyState
        title="还没有配置 Key"
        hint="支持 OpenAI 兼容协议任意 Base URL"
        action={<button type="button">添加</button>}
      />,
    );
    expect(screen.getByText("还没有配置 Key")).toBeTruthy();
    expect(screen.getByText(/OpenAI 兼容/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "添加" })).toBeTruthy();
  });

  it("异常态：可读详情与重试动作", () => {
    const onRetry = () => {};
    render(<ErrorState title="账单加载失败" detail="HTTP 503" onRetry={onRetry} />);
    expect(screen.getByText("账单加载失败")).toBeTruthy();
    expect(screen.getByText(/HTTP 503/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("断网态：默认文案与自定义提示", () => {
    render(<OfflineState />);
    expect(screen.getByText(/断网模式/)).toBeTruthy();
    cleanup();
    render(<OfflineState hint="本地留痕可用" />);
    expect(screen.getByText(/本地留痕可用/)).toBeTruthy();
  });

  it("加载态", () => {
    render(<LoadingState />);
    expect(screen.getByText(/加载中/)).toBeTruthy();
  });
});