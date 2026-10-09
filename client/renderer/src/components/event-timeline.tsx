/**
 * 工具调用时间线（FE-TOOLUI W12）：tool.call/tool.result 归约结果的可视化。
 *
 * 工具名 + 状态（进行中/成功/失败红标）+ 耗时 + 结果引用；点击展开详情。
 * argsSummary 已由引擎侧脱敏（≤512 字符，禁止题面正文），客户端仅原样展示，
 * 与遥测同级脱敏口径（客户端方案 §6.2）。
 */
import { useState, type ReactNode } from "react";
import type { ToolTimelineItem } from "../state/engine-slice.ts";

export function EventTimeline(props: { items: ToolTimelineItem[] }): ReactNode {
  const [expanded, setExpanded] = useState<string | null>(null);
  if (props.items.length === 0) return null;

  return (
    <section className="tool-timeline">
      <h3>工具调用（{props.items.length}）</h3>
      <ul className="tool-list">
        {props.items.map((item) => {
          const open = expanded === item.callId;
          const stateClass = item.ok === null ? "pending" : item.ok ? "ok" : "fail";
          return (
            <li key={item.callId} className={`tool-item ${stateClass}`}>
              <button
                type="button"
                className="tool-row"
                onClick={() => setExpanded(open ? null : item.callId)}
              >
                <span className="tool-name">{item.tool}</span>
                <span className="tool-status">
                  {item.ok === null ? "进行中" : item.ok ? "✓" : "✗"}
                </span>
                {item.durationMs !== null ? (
                  <span className="tool-dur">{item.durationMs}ms</span>
                ) : null}
              </button>
              {open ? (
                <div className="tool-detail">
                  <div className="tool-args">{item.argsSummary || "（无参数摘要）"}</div>
                  {item.resultRef ? <div className="tool-ref">结果引用：{item.resultRef}</div> : null}
                  {item.error ? <div className="tool-err">错误：{item.error}</div> : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
