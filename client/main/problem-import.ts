import { copyFile, mkdir, readFile, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { inflateRawSync } from "node:zlib";
import { SaxesParser } from "saxes";
import { basename, extname, join } from "node:path";

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const MAX_TEXT_CHARS = 60000;
export interface ImportedProblem { title: string; problemText: string; sourceName: string }
export interface ProblemImportOptions {
  select: () => Promise<string | null>;
  ocrCachePath: string;
  extract?: (extension: string, bytes: Buffer) => Promise<string>;
}

/** DOCX 解压前检查中央目录的总大小，拒绝加密/ZIP64及过大的解压内容。 */
export function validateDocxBudget(bytes: Buffer): void {
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (bytes.readUInt32LE(i) === 0x06054b50) { end = i; break; }
  }
  if (end < 0 || bytes.readUInt16LE(end + 4) || bytes.readUInt16LE(end + 6)) throw new Error("Word 文件结构无效");
  const count = bytes.readUInt16LE(end + 10);
  let offset = bytes.readUInt32LE(end + 16);
  let expanded = 0;
  if (!count || count === 65535) throw new Error("不支持此 Word 压缩格式");
  for (let i = 0; i < count; i++) {
    if (offset + 46 > end || bytes.readUInt32LE(offset) !== 0x02014b50 || (bytes.readUInt16LE(offset + 8) & 1)) throw new Error("Word 压缩目录无效或已加密");
    expanded += bytes.readUInt32LE(offset + 24);
    if (expanded > 40 * 1024 * 1024) throw new Error("Word 解压内容过大，请拆分文件");
    offset += 46 + bytes.readUInt16LE(offset + 28) + bytes.readUInt16LE(offset + 30) + bytes.readUInt16LE(offset + 32);
  }
}

/** 只提取主文档纯文字。限制实际解压输出，DTD/外部关系均不加载。 */
export function extractDocxText(bytes: Buffer): string {
  validateDocxBudget(bytes);
  let xml: Buffer | null = null;
  for (let i = 0; i + 46 <= bytes.length; i++) {
    if (bytes.readUInt32LE(i) !== 0x02014b50) continue;
    const nameSize = bytes.readUInt16LE(i + 28);
    const name = bytes.subarray(i + 46, i + 46 + nameSize).toString("utf8");
    if (name !== "word/document.xml") continue;
    const offset = bytes.readUInt32LE(i + 42);
    const compressed = bytes.readUInt32LE(i + 20);
    const size = bytes.readUInt32LE(i + 24);
    const method = bytes.readUInt16LE(i + 10);
    if (size > 8 * 1024 * 1024 || offset + 30 > bytes.length || bytes.readUInt32LE(offset) !== 0x04034b50) throw new Error("Word 主文档过大或损坏");
    const start = offset + 30 + bytes.readUInt16LE(offset + 26) + bytes.readUInt16LE(offset + 28);
    if (start + compressed > bytes.length) throw new Error("Word 主文档越界");
    const data = bytes.subarray(start, start + compressed);
    if (method === 0) xml = data;
    else if (method === 8) xml = inflateRawSync(data, { maxOutputLength: 8 * 1024 * 1024 });
    else throw new Error("不支持此 Word 压缩算法");
    if (xml.length !== size) throw new Error("Word 主文档长度不匹配");
    break;
  }
  if (!xml) throw new Error("Word 缺少主文档");
  const parser = new SaxesParser({ xmlns: true });
  let result = "";
  let inText = false;
  parser.on("doctype", () => { throw new Error("Word 文档不允许 DTD"); });
  parser.on("opentag", tag => { if (tag.uri === "http://schemas.openxmlformats.org/wordprocessingml/2006/main" && tag.local === "t") inText = true; });
  parser.on("text", text => { if (inText) result += text; if (result.length > MAX_TEXT_CHARS) throw new Error("Word 文字超过 6 万字"); });
  parser.on("closetag", tag => { if (tag.local === "t") inText = false; if (tag.local === "p") result += "\n"; });
  parser.write(new TextDecoder("utf-8", { fatal: true }).decode(xml)).close();
  return result;
}

/** 读取图片头后限制解码像素数量，避免小压缩文件触发过量内存分配。 */
export function validateImageBudget(bytes: Buffer): void {
  let width = 0; let height = 0;
  if (bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))) {
    width = bytes.readUInt32BE(16); height = bytes.readUInt32BE(20);
  } else if (bytes.length >= 4 && bytes.readUInt16BE(0) === 0xffd8) {
    let offset = 2;
    while (offset + 4 <= bytes.length) {
      if (bytes[offset] !== 0xff) break;
      const marker = bytes[offset + 1];
      const size = bytes.readUInt16BE(offset + 2);
      if (size < 2 || offset + 2 + size > bytes.length) break;
      if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker) && size >= 7) {
        height = bytes.readUInt16BE(offset + 5); width = bytes.readUInt16BE(offset + 7); break;
      }
      offset += 2 + size;
    }
  }
  if (!width || !height || width > 8000 || height > 8000 || width * height > 16000000) throw new Error("图片无效或超过1600万像素，请缩小图片后导入");
}

/** 主进程题面导入：原生选择文件、限制资源、提取纯文本；文件和 OCR 结果不上传。 */
export class ProblemImporter {
  private readonly options: ProblemImportOptions;
  private busy = false;
  constructor(options: ProblemImportOptions) { this.options = options; }
  async import(): Promise<ImportedProblem | null> {
    if (this.busy) throw new Error("题面导入正在进行，请稍候");
    this.busy = true;
    try {
      const path = await this.options.select();
      if (!path) return null;
      const extension = extname(path).toLowerCase();
      if (![".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg"].includes(extension)) throw new Error("支持 TXT、Markdown、PDF、Word DOCX 和 PNG/JPEG 图片");
      const info = await stat(path);
      if (!info.isFile() || !info.size || info.size > MAX_FILE_BYTES) throw new Error("文件为空或超过 20MB，请拆分后导入");
      const bytes = await readFile(path);
      if (bytes.length > MAX_FILE_BYTES) throw new Error("文件超过 20MB");
      if (extension === ".docx") validateDocxBudget(bytes);
      const extracted = this.options.extract ? await this.options.extract(extension, bytes) : await this.extract(extension, bytes);
      const problemText = extracted.replace(/^\uFEFF/, "").trim();
      if (!problemText || problemText.includes("\u0000")) throw new Error("未识别到有效文字；扫描 PDF 请改为图片导入，或手动粘贴题面");
      if (problemText.length > MAX_TEXT_CHARS) throw new Error("题面超过 6 万字，请拆分后导入");
      return { title: basename(path, extension).slice(0, 128), sourceName: basename(path), problemText };
    } catch (error) {
      // 文件系统错误含绝对路径，只展示原因码，避免把用户路径送入日志/遥测。
      if (error && typeof error === "object" && "code" in error) throw new Error(`题面文件无法读取（${String(error.code)}）`, { cause: error });
      throw error;
    } finally { this.busy = false; }
  }
  private async extract(extension: string, bytes: Buffer): Promise<string> {
    if ([".txt", ".md"].includes(extension)) return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (extension === ".docx") {
      return extractDocxText(bytes);
    }
    if (extension === ".pdf") {
      const { getDocument } = await import("pdfjs-dist/legacy/build/pdf.mjs");
      const loading = getDocument({ data: new Uint8Array(bytes), useWorkerFetch: false, enableXfa: false,
        disableFontFace: true, useSystemFonts: false, verbosity: 0 });
      try {
        const doc = await loading.promise;
        if (doc.numPages > 100) throw new Error("PDF 超过 100 页，请选择需要的题面页");
        let text = "";
        for (let i = 1; i <= doc.numPages; i++) {
          const page = await doc.getPage(i);
          const content = await page.getTextContent();
          text += content.items.map(item => "str" in item ? item.str : "").join(" ") + "\n";
          page.cleanup();
          if (text.length > MAX_TEXT_CHARS) throw new Error("PDF 文字过多，请拆分题面");
        }
        return text;
      } finally { await loading.destroy(); }
    }
    validateImageBudget(bytes);
    const { createWorker } = await import("tesseract.js");
    const require = createRequire(import.meta.url);
    const languagesPath = join(this.options.ocrCachePath, "languages");
    await mkdir(languagesPath, { recursive: true });
    await Promise.all(["eng", "chi_sim"].map(async code => {
      const packageEntry = require.resolve(`@tesseract.js-data/${code}`);
      await copyFile(join(packageEntry, "..", "4.0.0_best_int", `${code}.traineddata.gz`), join(languagesPath, `${code}.traineddata.gz`));
    }));
    const worker = await createWorker(["eng", "chi_sim"], 1, {
      cachePath: this.options.ocrCachePath,
      langPath: languagesPath,
      gzip: true,
    });
    try { return (await worker.recognize(bytes)).data.text; }
    finally { await worker.terminate(); }
  }
}
