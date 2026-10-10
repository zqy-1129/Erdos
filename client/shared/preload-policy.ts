import { BRIDGE_CHANNELS } from "./bridge-channels.ts";
import { ENGINE_IPC_CHANNELS } from "./ipc.ts";
const events = new Set<string>([BRIDGE_CHANNELS.engineEvent, BRIDGE_CHANNELS.authSessionInvalidated]);
const invokes = new Set<string>([...Object.values(BRIDGE_CHANNELS), ...ENGINE_IPC_CHANNELS].filter(c => !events.has(c)));
/** 双层白名单：预加载端在进入 Electron IPC 前拒绝任意通道与方向错误。 */
export function assertBridgeChannel(channel: string, direction: "invoke" | "subscribe"): void {
  if (!(direction === "invoke" ? invokes : events).has(channel)) throw new Error("未授权的客户端通道");
}
