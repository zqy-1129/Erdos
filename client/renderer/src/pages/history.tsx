/**
 * 历史页（SP3-4 / US-003 检查点继续）：任务列表（虚拟列表长表），
 * 可续任务「继续」回工作台（不重复扣积分）。三态齐备。
 */

import { type ReactNode } from "react";
import { BRIDGE_CHANNELS, type HistoryTask } from "../bridges/bridge.ts";
import { EmptyState, ErrorState, OfflineState } from "../components/states.tsx";
import { useRemoteData } from "../components/use-remote.ts";
import { VirtualList } from "../components/virtual-list.tsx";
import { navigate } from "../router.tsx";
import { useStore } from "../storage/store.ts";
import type { AppStores } from "../state/app-stores.ts";

export function HistoryPage(props: { stores: AppStores }): ReactNode {
  const connectivity = useStore(props.stores.connectivity);
  const remote = useRemoteData<HistoryTask[]>(() =>
    props.stores.bridge.invoke<HistoryTask[]>(BRIDGE_CHANNELS.historyList),
  );

  if (remote.loading) return <div className="page"><h2>历史</h2><div className="state-card">加载中…</div></div>;
  if (remote.error) {
    return (
      <div className="page">
        <h2>历史</h2>
        {remote.errorKind === "network" && !connectivity.online ? (
          <OfflineState hint="断网模式下展示本地留痕；云端历史暂不可达。" />
        ) : (
          <ErrorState title="历史记录加载失败" detail={remote.error} onRetry={remote.reload} />
        )}
      </div>
    );
  }
  if (remote.data === null || remote.data.length === 0) {
    return (
      <div className="page">
        <h2>历史</h2>
        <EmptyState title="还没有任务" hint="跑过的题目会留在这里，中途断电可从检查点继续（不重复扣积分）。" />
      </div>
    );
  }

  const resume = async (task: HistoryTask) => {
    try {
      await props.stores.bridge.invoke(BRIDGE_CHANNELS.historyResume, { taskId: task.taskId });
      navigate("/workspace");
    } catch (error) {
      // 继续失败：停留在列表并提示
      alert(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="page">
      <h2>历史</h2>
      <VirtualList
        items={remote.data}
        rowHeight={56}
        height={420}
        renderRow={(task) => (
          <div className="history-row">
            <div className="history-main">
              <b>{task.title}</b>
              <span className="muted">{task.updatedAt.slice(0, 16).replace("T", " ")} · {task.status}</span>
            </div>
            <div>
              {task.resumable ? (
                <button type="button" className="btn" onClick={() => void resume(task)}>
                  从检查点继续
                </button>
              ) : (
                <span className="muted">已完成</span>
              )}
            </div>
          </div>
        )}
      />
    </div>
  );
}