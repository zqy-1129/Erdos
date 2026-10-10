import type { UpdateView } from "../../shared/update.ts";
export interface UpdatePort {
  on: (event: string, listener: (...args: unknown[]) => void) => unknown;
  check: () => Promise<unknown>; download: () => Promise<unknown>; install: () => void;
}
export interface UpdateOptions { enabled: boolean; reason?: string; version: string; port: UpdatePort; canInstall: () => Promise<boolean> }
/** 更新状态与副作用分离；下载和安装必须分别由用户触发，任务执行中拒绝安装。 */
export class UpdateController {
  private readonly options: UpdateOptions;
  private state: UpdateView;
  private busy = false;
  constructor(options: UpdateOptions) {
    this.options = options;
    this.state = { status: options.enabled ? "idle" : "disabled", currentVersion: options.version,
      version: null, progress: 0, message: options.reason ?? "" };
    options.port.on("update-available", value => {
      const info = value as { version?: string };
      this.state = { ...this.state, status: "available", version: info.version ?? null, message: "发现新版本，可手动下载" };
    });
    options.port.on("update-not-available", () => { this.state = { ...this.state, status: "current", message: "已是最新版本" }; });
    options.port.on("download-progress", value => {
      const percent = Number((value as { percent?: number }).percent);
      this.state = { ...this.state, progress: Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0 };
    });
    options.port.on("update-downloaded", () => { this.state = { ...this.state, status: "downloaded", progress: 100, message: "下载完成，可在结束任务后安装" }; });
    options.port.on("error", () => { this.state = { ...this.state, status: "error", message: "更新服务暂不可用，请重试" }; });
  }
  view(): UpdateView { return { ...this.state }; }
  async run(action: "check" | "download" | "install"): Promise<UpdateView> {
    if (!this.options.enabled) throw new Error(this.state.message || "当前渠道禁用更新");
    if (this.busy) throw new Error("更新操作正在进行");
    if (action === "download" && this.state.status !== "available") throw new Error("请先检查可用更新");
    if (action === "install" && this.state.status !== "downloaded") throw new Error("请先下载更新");
    this.busy = true;
    try {
      if (action === "install") {
        if (!await this.options.canInstall()) throw new Error("任务正在运行或暂停，请先结束任务");
        this.options.port.install();
      } else {
        this.state = { ...this.state, status: action === "check" ? "checking" : "downloading", message: "" };
        await (action === "check" ? this.options.port.check() : this.options.port.download());
      }
      return this.view();
    } catch (error) {
      this.state = { ...this.state, status: "error", message: error instanceof Error ? error.message : "更新失败" };
      // 已下载的文件可在任务结束后重试安装。
      if (action === "install") this.state.status = "downloaded";
      throw new Error(this.state.message, { cause: error });
    } finally { this.busy = false; }
  }
}
