/**
 * 应用入口与路由表（SP3-4）：hash 路由 → 页面映射 → AppShell。
 * 未登录一律回登录页；桥经 createBridge 工厂选择（preload 真实桥优先，dev 回退演示桥）。
 */

import { createRoot } from "react-dom/client";
import type { ReactNode } from "react";
import type { ErdosBridge } from "./bridges/bridge.ts";
import { createBridge } from "./bridges/bridge-factory.ts";
import { BootTimer } from "./engine/boot-mark.ts";
import { CrashGuard } from "./engine/crash-guard.ts";
import { AppShell } from "./components/layout.tsx";
import { useHashRoute } from "./router.tsx";
import { createAppStores, type AppStores } from "./state/app-stores.ts";
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
  return createAppStores(bridge);
}

// 浏览器入口（组件测试不执行：无 document 环境）
if (typeof document !== "undefined") {
  const rootElement = document.getElementById("root");
  if (rootElement) {
    const bootTimer = new BootTimer();
    bootTimer.mark("boot"); // 冷启动计时起点（SP3-7 非功能基线）
    const crashGuard = new CrashGuard(); // 崩溃率基线统计（PRD：崩溃率 <0.5%）
    crashGuard.attach();
    const stores = createStores(createBridge());
    createRoot(rootElement).render(<App stores={stores} />);
    bootTimer.mark("first-render"); // 首屏挂载完成
    // 冷启动/崩溃基线（演示环境控制台可见；设置页展示）
    (globalThis as { __erdosBoot?: BootTimer; __erdosCrash?: CrashGuard }).__erdosBoot = bootTimer;
    (globalThis as { __erdosCrash?: CrashGuard }).__erdosCrash = crashGuard;
  }
}