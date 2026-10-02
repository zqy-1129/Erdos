/**
 * 虚拟列表组件测试（SP3-4）：只渲染可视窗口 + overscan；滚动后窗口滑动。
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { VirtualList } from "../src/components/virtual-list.tsx";

afterEach(cleanup);

const ROWS = Array.from({ length: 100 }, (_, i) => `row-${i}`);

describe("VirtualList 窗口渲染", () => {
  it("初始只渲染可视窗口 + overscan，长列表不整表渲染", () => {
    const { container } = render(
      <VirtualList items={ROWS} rowHeight={20} height={100} overscan={4} renderRow={(row) => <span>{row}</span>} />,
    );
    const rendered = Array.from(container.querySelectorAll("[role='listitem']"));
    // 可视 5 行 + 上下 overscan 各 4 行 = 13 行（远小于 100）
    expect(rendered.length).toBe(13);
    expect(rendered[0].textContent).toBe("row-0");
    expect(rendered[12].textContent).toBe("row-12");
  });

  it("滚动后窗口滑动到对应区间", () => {
    const { container } = render(
      <VirtualList items={ROWS} rowHeight={20} height={100} overscan={4} renderRow={(row) => <span>{row}</span>} />,
    );
    const scroller = container.querySelector(".virtual") as HTMLElement;
    scroller.scrollTop = 1000; // 滚到第 50 行
    fireEvent.scroll(scroller);
    const rendered = Array.from(container.querySelectorAll("[role='listitem']"));
    const first = Number(rendered[0].textContent!.replace("row-", ""));
    const last = Number(rendered[rendered.length - 1].textContent!.replace("row-", ""));
    expect(first).toBe(46); // 50 - overscan 4
    expect(last).toBeLessThan(100);
    expect(rendered.length).toBe(13);
  });

  it("空列表渲染空态提示", () => {
    const { container } = render(
      <VirtualList items={[]} rowHeight={20} height={100} renderRow={() => <span>x</span>} emptyHint="暂无流水" />,
    );
    expect(container.textContent).toContain("暂无流水");
  });
});