/**
 * 导出落盘（SP3-5 应用层第三批）：原生保存对话框 + 写盘，供 billing:export 等导出通道接线。
 *
 * 语义：
 * - 用户取消 → { canceled: true, path: null }（不视为失败、不写盘）；
 * - 覆盖确认由原生对话框承担（Windows 保存框自带「是否替换」确认；用户确认后才 writeFile）；
 * - 写入失败（磁盘/权限）原样抛出，由桥归一为「导出保存失败：…」可读错误（渲染层展示）；
 * - 内容按 UTF-8 原样写盘（服务端 CSV 的 BOM 由调用方按契约补回，落盘不改写内容语义）。
 *
 * 本模块依赖 electron（dialog）与 node:fs（写盘）；桥侧经回调注入（ExportSaver），保持可测。
 */
import { dialog, type BrowserWindow, type SaveDialogOptions } from "electron";
import { writeFile } from "node:fs/promises";
import type { ExportSaver } from "./ipc/cloud-business.ts";

/**
 * 生成导出保存器：getWindow 返回父窗口（可为 null，如窗口未创建时）。
 * title 为保存对话框标题（如「导出积分流水（CSV）」）。
 */
export function createExportSaver(
  getWindow: () => BrowserWindow | null,
  title: string,
): ExportSaver {
  return async (suggestedName, content) => {
    const options: SaveDialogOptions = {
      title,
      defaultPath: suggestedName,
      filters: [{ name: "CSV 文件", extensions: ["csv"] }],
    };
    const window = getWindow();
    const result = window
      ? await dialog.showSaveDialog(window, options)
      : await dialog.showSaveDialog(options);
    if (result.canceled || !result.filePath) {
      return { canceled: true, path: null };
    }
    await writeFile(result.filePath, content, "utf-8");
    return { canceled: false, path: result.filePath };
  };
}