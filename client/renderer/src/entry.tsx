/**
 * 应用入口与路由表（SP3-4）：hash 路由 → 页面映射 → AppShell。
 * 未登录一律回登录页；Web 开发模式挂载 WebDemoBridge（真实 preload 留 SP3-7）。
 */

import { createRoot } from "react-dom/client";
import type { ReactNode } from "react";
import type { ErdosBridge } from "./bridges/bridge.ts";
import { WebDemoBridge } from "./bridges/web-bridge.ts";
import { AppShell } from "./components/layout.tsx";
import { useHashRoute } from "./router.tsx";
import { bindConnectivity, createAppStores, type AppStores } from "./state/app-stores.ts";
import { useStore } from "./storage/store.ts";
import { LoginPage } from "./pages/login.tsx";
import { WorkspacePage } from "./pages/workspace.tsx";
import { KeysPage } from "./pages/keys.tsx";
import { BillingPage } from "./pages/billing.tsx";
import { ContentPage } from "./pages/content.tsx";
import { HistoryPage } from "./pages/history.tsx";
import { CompliancePage } from "./pages/compliance.tsx";
import { SettingsPage } from "./pages/settings.tsx";
import "./styles.css";

export function App(props: { stores: AppStores }): ReactNode {
  const route = useHashRoute();
  const session = useStore(props.stores.session);

  let page: ReactNode;
  if (session.status !== "signed-in") {
    page = <LoginPage stores={props.stores} />;
  } else {
    switch (route) {
      case "/keys":
        page = <KeysPage stores={props.stores} />;
        break;
      case "/billing":
        page = <BillingPage stores={props.stores} />;
        break;
      case "/content":
        page = <ContentPage stores={props.stores} />;
        break;
      case "/history":
        page = <HistoryPage stores={props.stores} />;
        break;
      case "/compliance":
        page = <CompliancePage stores={props.stores} />;
        break;
      case "/settings":
        page = <SettingsPage stores={props.stores} />;
        break;
      case "/workspace":
      case "/":
      default:
        page = <WorkspacePage stores={props.stores} />;
    }
  }
  return <AppShell stores={props.stores}>{page}</AppShell>;
}

export function createStores(bridge: ErdosBridge): AppStores {
  const stores = createAppStores(bridge);
  bindConnectivity(stores);
  return stores;
}

// 浏览器入口（组件测试不执行：无 document 环境）
if (typeof document !== "undefined") {
  const rootElement = document.getElementById("root");
  if (rootElement) {
    const stores = createStores(new WebDemoBridge());
    createRoot(rootElement).render(<App stores={stores} />);
  }
}