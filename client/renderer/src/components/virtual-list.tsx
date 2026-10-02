/**
 * 自研固定行高虚拟列表（SP3-4）：长日志/长列表只渲染可视窗口 + overscan。
 * 执行计划要求虚拟列表用于长日志，且不整页刷新。
 */

import { useState, type ReactNode, type UIEvent } from "react";

export interface VirtualListProps<T> {
  items: T[];
  rowHeight: number;
  /** 可视区高度（px）。 */
  height: number;
  renderRow(item: T, index: number): ReactNode;
  /** 可视区外预渲染行数（默认 4）。 */
  overscan?: number;
  emptyHint?: string;
}

export function VirtualList<T>(props: VirtualListProps<T>): ReactNode {
  const overscan = props.overscan ?? 4;
  const [scrollTop, setScrollTop] = useState(0);

  if (props.items.length === 0) {
    return <div className="virtual-empty">{props.emptyHint ?? "暂无数据"}</div>;
  }

  const totalHeight = props.items.length * props.rowHeight;
  const start = Math.max(0, Math.floor(scrollTop / props.rowHeight) - overscan);
  const visibleCount = Math.ceil(props.height / props.rowHeight) + overscan * 2;
  const end = Math.min(props.items.length, start + visibleCount);
  const slice = props.items.slice(start, end);

  const onScroll = (event: UIEvent<HTMLDivElement>) => {
    setScrollTop(event.currentTarget.scrollTop);
  };

  return (
    <div
      className="virtual"
      style={{ height: props.height, overflowY: "auto" }}
      onScroll={onScroll}
      role="list"
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        <div style={{ position: "absolute", top: start * props.rowHeight, left: 0, right: 0 }}>
          {slice.map((item, offset) => {
            const index = start + offset;
            return (
              <div key={index} style={{ height: props.rowHeight }} role="listitem">
                {props.renderRow(item, index)}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}