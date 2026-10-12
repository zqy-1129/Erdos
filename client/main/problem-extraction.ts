import { copyFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { inflateRawSync } from "node:zlib";
import { SaxesParser } from "saxes";
import { join } from "node:path";
const MAX_TEXT_CHARS = 60000;
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


/** 文件解析在独立进程执行；进度回调只传数字。 */
export async function extractProblemText(extension: string, bytes: Buffer, ocrCachePath: string,
  onProgress: (progress: number) => void): Promise<string> {
  if ([".txt", ".md"].includes(extension)) return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  if (extension === ".docx") return extractDocxText(bytes);
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
        try { const content = await page.getTextContent(); text += content.items.map(item => "str" in item ? item.str : "").join(" ") + "\n"; }
        finally { page.cleanup(); }
        if (text.length > MAX_TEXT_CHARS) throw new Error("PDF 文字过多，请拆分题面");
        onProgress(i / doc.numPages);
      }
      return text;
    } finally { await loading.destroy(); }
  }
  validateImageBudget(bytes);
  const { createWorker } = await import("tesseract.js");
  const require = createRequire(import.meta.url);
  const languagesPath = join(ocrCachePath, "languages");
  await mkdir(languagesPath, { recursive: true });
  await Promise.all(["eng", "chi_sim"].map(async code => {
    const packageEntry = require.resolve(`@tesseract.js-data/${code}`);
    await copyFile(join(packageEntry, "..", "4.0.0_best_int", `${code}.traineddata.gz`), join(languagesPath, `${code}.traineddata.gz`));
  }));
  const worker = await createWorker(["eng", "chi_sim"], 1, { cachePath: ocrCachePath, langPath: languagesPath, gzip: true,
    logger: event => { if (event.status === "recognizing text") onProgress(event.progress); } });
  try { return (await worker.recognize(bytes)).data.text; }
  finally { await worker.terminate(); }
}
