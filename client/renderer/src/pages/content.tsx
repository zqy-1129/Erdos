/**
 * 内容库（SP3-4 / US-008）：模板库/案例库 tab + 检索；
 * 案例注入带「仅作参照」合规提示。三态齐备。
 */

import { useMemo, useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS, type ContentItem } from "../bridges/bridge.ts";
import { EmptyState, ErrorState, OfflineState } from "../components/states.tsx";
import { useRemoteData } from "../components/use-remote.ts";
import { useStore } from "../storage/store.ts";
import type { AppStores } from "../state/app-stores.ts";

export function ContentPage(props: { stores: AppStores }): ReactNode {
  const connectivity = useStore(props.stores.connectivity);
  const [tab, setTab] = useState<"template" | "case">("template");
  const [query, setQuery] = useState("");
  const remote = useRemoteData<ContentItem[]>(() =>
    props.stores.bridge.invoke<ContentItem[]>(BRIDGE_CHANNELS.contentList),
  );

  const filtered = useMemo(() => {
    const items = (remote.data ?? []).filter((item) => item.kind === tab);
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (item) => item.title.toLowerCase().includes(q) || item.tags.some((tag) => tag.toLowerCase().includes(q)),
    );
  }, [remote.data, tab, query]);

  if (remote.loading) return <div className="page"><h2>内容库</h2><div className="state-card">加载中…</div></div>;
  if (remote.error) {
    return (
      <div className="page">
        <h2>内容库</h2>
        {remote.errorKind === "network" && !connectivity.online ? (
          <OfflineState />
        ) : (
          <ErrorState title="内容库加载失败" detail={remote.error} onRetry={remote.reload} />
        )}
      </div>
    );
  }

  return (
    <div className="page">
      <h2>内容库</h2>
      <div className="tabs">
        <button type="button" className={`tab ${tab === "template" ? "active" : ""}`} onClick={() => setTab("template")}>
          模板库
        </button>
        <button type="button" className={`tab ${tab === "case" ? "active" : ""}`} onClick={() => setTab("case")}>
          案例库
        </button>
        <input
          className="search"
          placeholder="按题型/方法检索，如：优化、格式"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {tab === "case" ? (
        <div className="compliance-tip">合规提示：案例内容仅供思路参照，请勿直接搬运文本。</div>
      ) : null}
      {filtered.length === 0 ? (
        <EmptyState
          title={query ? "没有匹配的内容" : tab === "template" ? "模板库为空" : "案例库为空"}
          hint={query ? "换个关键词试试。" : "模板库含 CUMCM/MCM 官方格式；案例库按题型/方法可检索。"}
        />
      ) : (
        <ul className="content-list">
          {filtered.map((item) => (
            <li key={item.id} className="content-item">
              <div className="content-title">
                <span className="tag">{item.kind === "template" ? "模板" : "案例"}</span>
                {item.title}
                {item.referenceOnly ? <span className="ref-only">仅作参照</span> : null}
              </div>
              {item.complianceNote ? <p className="compliance-tip">{item.complianceNote}</p> : null}
              <div className="content-tags">
                {item.tags.map((tag) => (
                  <span key={tag} className="tag-ghost">{tag}</span>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
