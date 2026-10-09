/**
 * 导出落盘（SP3-5 应用层第三批 / SP3-6 收尾第二批）：原生保存对话框 + 写盘，
 * 供 billing:export（CSV 文本）与 compliance:save（md/latex 文本 + docx 真实二进制）等导出通道接线。
 *
 * 语义：
 * - 用户取消 → { canceled: true, path: null }（不视为失败、不写盘）；
 * - 覆盖确认由原生对话框承担（Windows 保存框自带「是否替换」确认；用户确认后才 writeFile）；
 * - 写入失败（磁盘/权限）原样抛出，由调用方归一为「导出保存失败：…」可读错误（渲染层展示）；
 * - 文本按 UTF-8 原样写盘（服务端 CSV 的 BOM 由调用方按契约补回，落盘不改写内容语义）；
 *   二进制（docx）按字节原样写盘。
 *
 * 本模块依赖 electron（dialog）与 node:fs（写盘）；桥侧经回调注入（ExportSaver /
 * DeclarationFileSaver），保持可测。
 */
import { dialog, type BrowserWindow, type SaveDialogOptions } from "electron";
import { writeFile } from "node:fs/promises";
import type { DeclarationFileSaver } from "./engine-trail.ts";
import type { ExportSaver } from "./ipc/cloud-business.ts";

/**
 * 生成通用文件保存器：getWindow 返回父窗口（可为 null）。
 * 保存对话框标题 title（如「导出 AI 工具使用声明」）；文件类型由请求携带的滤镜决定。
 */
export function createFileSaver(
  getWindow: () => BrowserWindow | null,
  title: string,
): DeclarationFileSaver {
  return async (request) => {
    const options: SaveDialogOptions = {
      title,
      defaultPath: request.suggestedName,
      filters: [{ name: request.filterName, extensions: request.extensions }],
    };
    const window = getWindow();
    const result = window
      ? await dialog.showSaveDialog(window, options)
      : await dialog.showSaveDialog(options);
    if (result.canceled || !result.filePath) {
      return { canceled: true, path: null };
    }
    await writeFile(result.filePath, request.content);
    return { canceled: false, path: result.filePath };
  };
}

/**
 * 生成 CSV 导出保存器（billing:export）：保持既有签名（ExportSaver），
 * 对话框/写盘逻辑由通用 createFileSaver 单一实现（避免两份落盘代码漂移）。
 */
export function createExportSaver(
  getWindow: () => BrowserWindow | null,
  title: string,
): ExportSaver {
  const save = createFileSaver(getWindow, title);
  return (suggestedName, content) =>
    save({ suggestedName, content, filterName: "CSV 文件", extensions: ["csv"] });
}