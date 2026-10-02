/**
 * 应用壳（SP3-4）：未登录 → 登录页；已登录 → 侧栏导航 + 内容区。
 * 断网横幅全局常驻（每页断网态）；导航用 hash 路由自研实现。
 */

import { useEffect, type ReactNode } from "react";
import { useHashRoute } from "../router.tsx";
import { useStore } from "../storage/store.ts";
import { refreshEntitlementAction, type AppStores } from "../state/app-stores.ts";
import { OfflineBanner } from "./offline-banner.tsx";

const NAV: Array<{ path: string; label: string }> = [
  { path: "/workspace", label: "工作台" },
  { path: "/keys", label: "Key 管理" },
  { path: "/billing", label: "账单" },
  { path: "/content", label: "内容库" },
  { path: "/history", label: "历史" },
  { path: "/compliance", label: "合规导出" },
  { path: "/settings", label: "设置" },
];

export function AppShell(props: { stores: AppStores; children: ReactNode }): ReactNode {
  const { stores } = props;
  const session = useStore(stores.session);
  const route = useHashRoute();

  // 登录后拉取权益快照（断网态横幅数据源）
  useEffect(() => {
    if (session.status === "signed-in") {
      void refreshEntitlementAction(stores);
    }
  }, [session.status, stores]);

  if (session.status !== "signed-in") {
    return <div className="shell">{props.children}</div>;
  }

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">Erdos</div>
        <nav>
          {NAV.map((item) => (
            <a
              key={item.path}
              href={`#${item.path}`}
              className={`nav-item ${route === item.path ? "active" : ""}`}
            >
              {item.label}
            </a>
          ))}
        </nav>
      </aside>
      <main className="main">
        <OfflineBanner connectivity={stores.connectivity} entitlement={stores.entitlement} />
        {props.children}
      </main>
    </div>
  );
}