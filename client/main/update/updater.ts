import { autoUpdater } from "electron-updater";
import { isAutoUpdateEnabled, type UpdateChannel } from "./policy.ts";
import { UpdateController } from "./controller.ts";
export interface UpdaterOptions {
  channel: UpdateChannel; onLog: (line: string) => void; checkOnStart?: boolean;
  version: string; packaged: boolean; feedUrl?: string; canInstall: () => Promise<boolean>;
}
/** 运行时适配器：禁用自动下载/退出安装；仅正式安装包与已配置 HTTPS 发布源启用。 */
export function setupAutoUpdate(options: UpdaterOptions): UpdateController {
  let validFeed = false;
  try {
    const url = new URL(options.feedUrl ?? "");
    validFeed = url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash;
  } catch { /* 发布源未配置。 */ }
  const enabled = isAutoUpdateEnabled(options.channel) && options.packaged && validFeed;
  const reason = !isAutoUpdateEnabled(options.channel) ? "当前渠道禁用更新" :
    !options.packaged ? "开发版本暂不支持安装更新" : "更新发布服务尚未配置";
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  if (enabled) autoUpdater.setFeedURL({ provider: "generic", url: options.feedUrl! });
  const controller = new UpdateController({ enabled, reason: enabled ? "" : reason, version: options.version,
    canInstall: options.canInstall, port: {
      on: (event, listener) => autoUpdater.on(event as Parameters<typeof autoUpdater.on>[0], listener),
      check: () => autoUpdater.checkForUpdates(),
      download: () => autoUpdater.downloadUpdate(),
      install: () => autoUpdater.quitAndInstall(false, true),
    } });
  if (enabled && options.checkOnStart !== false) void controller.run("check").catch(() => options.onLog("[update] 检查失败"));
  return controller;
}
