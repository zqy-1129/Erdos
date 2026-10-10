import { createHash } from "node:crypto";
import { basename, extname, relative, resolve, isAbsolute } from "node:path";
import { readFile, realpath, stat } from "node:fs/promises";
import type { ArtifactEntry, TrailSource } from "../declaration/types.ts";
import type { ArtifactPreview, ArtifactView } from "../shared/artifact.ts";
import type { DeclarationFileSaver } from "./engine-trail.ts";
export interface ArtifactBrowserOptions { home: string; source: Pick<TrailSource, "artifacts">; save: DeclarationFileSaver }
/** 只能按留痕索引读取 home 内的普通文件；读取后验 SHA256。复杂度 O(n + 文件字节数)。 */
export class ArtifactBrowser {
  private readonly options: ArtifactBrowserOptions;
  constructor(options: ArtifactBrowserOptions) { this.options = options; }
  private async entries(taskId: unknown): Promise<ArtifactEntry[]> {
    if (typeof taskId !== "string" || !/^[\w-]{1,64}$/.test(taskId)) throw new Error("任务号无效");
    return this.options.source.artifacts(taskId);
  }
  async list(taskId: unknown): Promise<ArtifactView[]> {
    return (await this.entries(taskId)).map((entry, index) => ({
      id: entry.sha256 + "." + index, name: basename(entry.file_path), stage: entry.stage,
      kind: entry.kind, sha256: entry.sha256, size: entry.size_bytes,
    }));
  }
  private async read(taskId: unknown, id: unknown): Promise<{ entry: ArtifactEntry; bytes: Buffer }> {
    const entries = await this.entries(taskId);
    const entry = entries.find((e, i) => e.sha256 + "." + i === id);
    if (!entry) throw new Error("产物不存在，请刷新列表");
    const home = await realpath(this.options.home);
    const path = await realpath(resolve(home, entry.file_path));
    const rel = relative(home, path);
    if (!rel || rel.startsWith("..") || isAbsolute(rel)) throw new Error("产物路径超出任务数据目录");
    const info = await stat(path);
    if (!info.isFile() || info.size > 20 * 1024 * 1024) throw new Error("产物超过预览或导出大小限制");
    const bytes = await readFile(path);
    if (bytes.length > 20 * 1024 * 1024 || createHash("sha256").update(bytes).digest("hex") !== entry.sha256) throw new Error("产物哈希校验失败，文件可能已被修改");
    return { entry, bytes };
  }
  async preview(taskId: unknown, id: unknown): Promise<ArtifactPreview> {
    const { entry, bytes } = await this.read(taskId, id);
    const name = basename(entry.file_path);
    const extension = extname(name).toLowerCase();
    if ([".png", ".jpg", ".jpeg"].includes(extension)) return { kind: "image", name,
      imageUrl: "data:image/" + (extension === ".png" ? "png" : "jpeg") + ";base64," + bytes.toString("base64") };
    if ([".md", ".txt", ".tex", ".csv", ".json", ".py"].includes(extension)) {
      if (bytes.length > 2 * 1024 * 1024) throw new Error("文本预览超过 2MB，请导出查看");
      return { kind: "text", name, text: new TextDecoder("utf-8", { fatal: true }).decode(bytes) };
    }
    return { kind: "binary", name };
  }
  async save(taskId: unknown, id: unknown): Promise<unknown> {
    const { entry, bytes } = await this.read(taskId, id);
    return this.options.save({ suggestedName: basename(entry.file_path), content: bytes, filterName: "任务产物", extensions: [extname(entry.file_path).slice(1) || "bin"] });
  }
}
