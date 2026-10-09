/**
 * 自动更新接线（FE-PKG W18 / F-102）：electron-updater（generic provider + latest.yml）。
 *
 * 契约（方案 §9）：
 * - 渠道门禁：preview / dev 不发起检查（见 policy.isAutoUpdateEnabled）；
 * - update 元数据 latest.yml 由 electron-builder 在打包阶段生成（publish.generic）；
 * - 先检查、**由用户确认后再下载**（autoDownload=false，避免流量与中断惊讶）；
 * - 安装走 electron-updater 的原子替换，NSIS 安装包保留上一版本以便回滚；
 * - 任何失败仅记日志，不阻塞主流程（更新不可用不得影响建模任务）。
 *
 * 本模块 import electron-updater（依赖 electron 运行时），不做 node:test 单测；
 * 可单测的策略全部收敛在 policy.ts。
 */
import { autoUpdater } from "electron-updater";
import { isAutoUpdateEnabled, type UpdateChannel } from "./policy.ts";

export interface UpdaterOptions {
  channel: UpdateChannel;
  onLog: (line: string) => void;
  /** 启动后是否立即检查（默认 true）。 */
  checkOnStart?: boolean;
}

/** 装配自动更新（按渠道门禁；preview/dev 直接短路）。 */
export function setupAutoUpdate(options: UpdaterOptions): void {
  if (!isAutoUpdateEnabled(options.channel)) {
    options.onLog(`[update] 渠道 ${options.channel} 禁用自动更新（F-102 产品约束）`);
    return;
  }
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("error", (error) => options.onLog(`[update] 错误：${error.message}`));
  autoUpdater.on("update-available", (info) => options.onLog(`[update] 发现新版本 ${info.version}（待用户确认下载）`));
  autoUpdater.on("update-not-available", () => options.onLog("[update] 已是最新版本"));
  autoUpdater.on("update-downloaded", (info) => options.onLog(`[update] 新版本 ${info.version} 已下载，退出时安装`));
  if (options.checkOnStart !== false) {
    void autoUpdater.checkForUpdates().catch((error: unknown) => {
      options.onLog(`[update] 检查失败：${error instanceof Error ? error.message : String(error)}`);
    });
  }
}

/** 用户确认后下载更新（返回是否成功；失败仅记日志）。 */
export async function downloadUpdate(onLog: (line: string) => void): Promise<boolean> {
  try {
    await autoUpdater.downloadUpdate();
    return true;
  } catch (error) {
    onLog(`[update] 下载失败：${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}