/**
 * preload（FE-PRELOAD W4）：contextBridge 最小白名单桥。
 *
 * 安全基线（客户端开发详细方案 §4.1）：
 * - 只暴露 invoke（请求-响应）与 subscribe（事件订阅，返回退订函数）两个能力；
 * - 不暴露事件发送（send）/ Node / 文件 / 进程能力；
 * - 渲染层经 window.erdos 访问，通道名全部引用 shared/ipc.ts 常量（禁字面量）。
 */
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

const erdosBridge = {
  invoke<T = unknown>(channel: string, payload?: unknown): Promise<T> {
    return ipcRenderer.invoke(channel, payload) as Promise<T>;
  },
  subscribe(channel: string, handler: (payload: unknown) => void): () => void {
    const listener = (_event: IpcRendererEvent, payload: unknown): void => {
      handler(payload);
    };
    ipcRenderer.on(channel, listener);
    return () => {
      ipcRenderer.removeListener(channel, listener);
    };
  },
};

contextBridge.exposeInMainWorld("erdos", erdosBridge);
