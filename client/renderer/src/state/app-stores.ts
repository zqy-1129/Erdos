/**
 * 应用状态装配（SP3-4）：session / entitlement / connectivity / engine 四切片。
 * 全部经桥获取数据；断网态 UI（宽限倒计时、快照余额）由 entitlement 切片驱动。
 */

import type { EntitlementView, ErdosBridge, SessionView } from "../bridges/bridge.ts";
import { BRIDGE_CHANNELS } from "../bridges/bridge.ts";
import type { Store } from "../storage/store.ts";
import { createStore } from "../storage/store.ts";
import { EngineEventPump } from "../engine/event-source.ts";
import { createEngineStore, type EngineStoreHandle, type EngineViewState } from "./engine-slice.ts";

// ---------------------------------------------------------------------------
// 会话
// ---------------------------------------------------------------------------

export interface SessionState {
  status: "anonymous" | "signed-in";
  username: string | null;
  error: string | null;
}

export const initialSession: SessionState = { status: "anonymous", username: null, error: null };

// ---------------------------------------------------------------------------
// 权益（断网态数据源）
// ---------------------------------------------------------------------------

export interface EntitlementState extends EntitlementView {
  /** 最近一次拉取是否失败（网络/服务不可用）。 */
  stale: boolean;
}

export const initialEntitlement: EntitlementState = {
  status: "empty",
  balance: 0,
  graceDeadlineMs: null,
  stale: false,
};

// ---------------------------------------------------------------------------
// 连通性（window online/offline + 手动刷新）
// ---------------------------------------------------------------------------

export interface ConnectivityState {
  online: boolean;
}

// ---------------------------------------------------------------------------
// 聚合装配
// ---------------------------------------------------------------------------

export interface AppStores {
  bridge: ErdosBridge;
  session: Store<SessionState>;
  entitlement: Store<EntitlementState>;
  connectivity: Store<ConnectivityState>;
  engine: Store<EngineViewState>;
  pump: EngineEventPump;
  /** 全局释放（事件订阅清理）。 */
  dispose(): void;
}

/** 会话失效下发订阅（主进程业务 401 清会话后推送 → 回登录页并提示可读原因）。 */
export function bindSessionInvalidation(
  stores: Pick<AppStores, "session" | "bridge">,
): () => void {
  return stores.bridge.subscribe(BRIDGE_CHANNELS.authSessionInvalidated, () => {
    // 仅登录态回落：并发 401 的重复下发、登出后迟到的 in-flight 事件均不再产生噪声
    if (stores.session.getState().status !== "signed-in") return;
    stores.session.setState({ status: "anonymous", username: null, error: "登录已失效，请重新登录" });
  });
}

export function createAppStores(bridge: ErdosBridge): AppStores {
  const session = createStore<SessionState>(initialSession);
  const entitlement = createStore<EntitlementState>(initialEntitlement);
  const connectivity = createStore<ConnectivityState>({ online: typeof navigator === "undefined" ? true : navigator.onLine });

  const pump = new EngineEventPump(bridge);
  const engineHandle: EngineStoreHandle = createEngineStore(pump);
  const stopPump = pump.start();
  const stopSessionInvalidation = bindSessionInvalidation({ session, bridge });
  const stopConnectivity = bindConnectivity({ connectivity });

  const disposers: Array<() => void> = [stopPump, stopSessionInvalidation, stopConnectivity];
  return {
    bridge,
    session,
    entitlement,
    connectivity,
    engine: engineHandle.store,
    pump,
    dispose() {
      for (const dispose of disposers) dispose();
    },
  };
}

// ---------------------------------------------------------------------------
// 会话动作（页面调用；全部经桥，不直接触云）
// ---------------------------------------------------------------------------

export async function loginAction(
  stores: Pick<AppStores, "session" | "bridge">,
  username: string,
  password: string,
): Promise<void> {
  try {
    const view = await stores.bridge.invoke<SessionView>(BRIDGE_CHANNELS.authLogin, { username, password });
    stores.session.setState({ status: "signed-in", username: view.username, error: null });
  } catch (error) {
    stores.session.setState({ error: error instanceof Error ? error.message : String(error) });
  }
}

/**
 * 启动会话恢复（SP3-4 本地安全存储）：主进程已持久化令牌时免登录进入。
 * 失败（未配置云端/网络异常/无会话）一律保持匿名，不阻塞启动、不打扰用户。
 */
export async function restoreSessionAction(
  stores: Pick<AppStores, "session" | "bridge">,
): Promise<void> {
  try {
    const view = await stores.bridge.invoke<SessionView | null>(BRIDGE_CHANNELS.authSession);
    // 仅匿名态写入：恢复响应迟到时不覆盖用户刚完成的登录（竞态守卫）
    if (view && stores.session.getState().status !== "signed-in") {
      stores.session.setState({ status: "signed-in", username: view.username, error: null });
    }
  } catch {
    // 会话恢复属尽力而为：失败保持登录页
  }
}

/** 注册即登录（与 loginAction 同构：失败态可读回显；密码强度由本地门禁 + 服务端校验）。 */
export async function registerAction(
  stores: Pick<AppStores, "session" | "bridge">,
  username: string,
  password: string,
): Promise<void> {
  try {
    const view = await stores.bridge.invoke<SessionView>(BRIDGE_CHANNELS.authRegister, { username, password });
    stores.session.setState({ status: "signed-in", username: view.username, error: null });
  } catch (error) {
    stores.session.setState({ error: error instanceof Error ? error.message : String(error) });
  }
}

export function logoutAction(
  stores: Pick<AppStores, "session" | "bridge">,
): void {
  // 本地先回落；云端吊销失败（桥侧已告警）不产生未处理拒绝
  void stores.bridge.invoke(BRIDGE_CHANNELS.authLogout, {}).catch(() => {});
  stores.session.setState({ status: "anonymous", username: null, error: null });
}

/** 拉取权益视图（断网时保留上次快照并标记 stale）。 */
export async function refreshEntitlementAction(
  stores: Pick<AppStores, "entitlement" | "bridge">,
): Promise<void> {
  try {
    const view = await stores.bridge.invoke<EntitlementView>(BRIDGE_CHANNELS.entitlementStatus);
    stores.entitlement.setState({ ...view, stale: false });
  } catch {
    stores.entitlement.setState({ stale: true });
  }
}

/** 连通性监听（window online/offline；测试环境可注入事件源）。 */
export function bindConnectivity(
  stores: Pick<AppStores, "connectivity">,
  target: Pick<Window, "addEventListener" | "removeEventListener"> = window,
): () => void {
  const online = () => stores.connectivity.setState({ online: true });
  const offline = () => stores.connectivity.setState({ online: false });
  target.addEventListener("online", online);
  target.addEventListener("offline", offline);
  return () => {
    target.removeEventListener("online", online);
    target.removeEventListener("offline", offline);
  };
}