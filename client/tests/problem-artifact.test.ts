import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { mkdtemp, writeFile, rm, mkdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Document, Packer, Paragraph } from "docx";
import { ProblemImporter, extractDocxText, validateDocxBudget, validateImageBudget } from "../main/problem-import.ts";
import { ArtifactBrowser } from "../main/artifact-browser.ts";
import type { ArtifactEntry } from "../declaration/types.ts";
let temp: string;
before(async () => { temp = await mkdtemp(join(tmpdir(), "erdos-product-tests-")); });
after(async () => { await rm(temp, { recursive: true, force: true }); });
const importer = (path: string | null, extract?: (extension: string, bytes: Buffer) => Promise<string>) =>
  new ProblemImporter({ select: async () => path, ocrCachePath: join(temp, "ocr"), extract });
describe("真实题面文件导入", () => {
  it("TXT、Markdown、真实 DOCX 与文本 PDF 提取", async () => {
    for (const extension of [".txt", ".md"]) {
      const path = join(temp, "题面" + extension); await writeFile(path, "\ufeff 拟合题面\n数据1 ");
      assert.equal((await importer(path).import())?.problemText, "拟合题面\n数据1");
    }
    const docx = await Packer.toBuffer(new Document({ sections: [{ children: [new Paragraph("数学建模 & data"), new Paragraph("第二段")] }] }));
    const path = join(temp, "题面.docx"); await writeFile(path, docx);
    const imported = await importer(path).import(); assert.match(imported!.problemText, /数学建模 & data/); assert.match(imported!.problemText, /第二段/);
    const objects = [
      "<< /Type /Catalog /Pages 2 0 R >>",
      "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
      "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
      "<< /Length 44 >>\nstream\nBT /F1 24 Tf 40 700 Td (Fit y=a*x+b) Tj ET\nendstream",
    ];
    let pdf = "%PDF-1.4\n"; const offsets = [0];
    for (let i = 0; i < objects.length; i++) { offsets.push(Buffer.byteLength(pdf)); pdf += (i + 1) + " 0 obj\n" + objects[i] + "\nendobj\n"; }
    const xref = Buffer.byteLength(pdf); pdf += "xref\n0 6\n0000000000 65535 f \n" + offsets.slice(1).map(n => String(n).padStart(10, "0") + " 00000 n \n").join("") +
      "trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + xref + "\n%%EOF";
    const pdfPath = join(temp, "题面.pdf"); await writeFile(pdfPath, pdf);
    assert.match((await importer(pdfPath).import())!.problemText, /Fit y=a\*x\+b/);
  });
  it("本地英中文 OCR worker 实际识别图片", async () => {
    const require = createRequire(import.meta.url);
    const { createCanvas } = require("@napi-rs/canvas") as { createCanvas: (w: number, h: number) => any };
    const canvas = createCanvas(700, 150); const context = canvas.getContext("2d");
    context.fillStyle = "white"; context.fillRect(0, 0, 700, 150); context.fillStyle = "black";
    context.font = "60px Arial"; context.fillText("TEST 12345", 30, 95);
    const path = join(temp, "ocr.png"); await writeFile(path, canvas.toBuffer("image/png"));
    assert.match((await importer(path).import())!.problemText, /TEST\s*12345/);
  });
  it("图片头尺寸与畸形输入在解码前拒绝", () => {
    assert.throws(() => validateImageBudget(Buffer.from("invalid")));
    const png = Buffer.alloc(24); Buffer.from("89504e470d0a1a0a", "hex").copy(png);
    png.writeUInt32BE(9000, 16); png.writeUInt32BE(100, 20); assert.throws(() => validateImageBudget(png));
    png.writeUInt32BE(5000, 16); png.writeUInt32BE(5000, 20); assert.throws(() => validateImageBudget(png));
    const jpeg = Buffer.from("ffd8ffc000110801000200", "hex"); const data = Buffer.concat([jpeg, Buffer.alloc(12)]); validateImageBudget(data);
    data.writeUInt16BE(1, 4); assert.throws(() => validateImageBudget(data));
  });
  it("取消、文件格式/大小、无有效文字、超长及并发导入", async () => {
    assert.equal(await importer(null).import(), null);
    await assert.rejects(importer(join(temp, "x.exe")).import(), /支持/);
    await assert.rejects(importer(join(temp, "absent.txt")).import(), /ENOENT/);
    const empty = join(temp, "empty.txt"); await writeFile(empty, ""); await assert.rejects(importer(empty).import(), /为空/);
    const path = join(temp, "bounds.txt"); await writeFile(path, "unit");
    for (const text of [" ", "\0", "x".repeat(60001)]) await assert.rejects(importer(path, async () => text).import());
    await writeFile(path, Buffer.alloc(20 * 1024 * 1024 + 1)); await assert.rejects(importer(path).import(), /20MB/);
    await writeFile(path, "unit");
    let release: ((s: string) => void) | undefined;
    const c = importer(path, () => new Promise(resolve => { release = resolve; }));
    const pending = c.import();
    await new Promise(resolve => setTimeout(resolve, 30));
    await assert.rejects(c.import(), /正在进行/); release!("题面"); await pending;
    for (const bytes of [Buffer.alloc(0), Buffer.alloc(22), Buffer.from("bad zip")]) assert.throws(() => validateDocxBudget(bytes));
    const docx = await Packer.toBuffer(new Document({ sections: [{ children: [new Paragraph("a")] }] }));
    const corrupt = Buffer.from(docx); const central = corrupt.indexOf(Buffer.from("504b0102", "hex"));
    corrupt.writeUInt32LE(100 * 1024 * 1024, central + 24); assert.throws(() => extractDocxText(corrupt), /过大/);
  });
});
describe("产物索引读取与导出", () => {
  it("预览文本和图片、二进制导出、哈希变更和越界路径拒绝", async () => {
    const home = join(temp, "home"); await mkdir(home);
    const entries: ArtifactEntry[] = [];
    for (const name of ["paper.md", "figure.png", "paper.docx", "large.txt"]) {
      const bytes = name === "large.txt" ? Buffer.alloc(2 * 1024 * 1024 + 1, 65) : Buffer.from("unit");
      const path = join(home, name); await writeFile(path, bytes);
      entries.push({ task_id: "t", stage: "writing", kind: "paper", file_path: path, size_bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
    }
    let saved = ""; const c = new ArtifactBrowser({ home, source: { artifacts: async () => entries }, save: async input => { saved = input.suggestedName; return { canceled: false, path: "test" }; } });
    const list = await c.list("t"); assert.equal(list[0].name, "paper.md");
    assert.equal((await c.preview("t", list[0].id)).text, "unit");
    assert.match((await c.preview("t", list[1].id)).imageUrl!, /^data:image\/png;base64,/);
    assert.equal((await c.preview("t", list[2].id)).kind, "binary");
    await c.save("t", list[0].id); assert.equal(saved, "paper.md");
    await assert.rejects(c.preview("t", list[3].id), /2MB/);
    await assert.rejects(c.preview("t", "unknown")); await assert.rejects(c.list("../t"));
    await writeFile(join(home, "paper.md"), "changed"); await assert.rejects(c.preview("t", list[0].id), /哈希/);
    entries[0].file_path = join(temp, "bounds.txt"); await assert.rejects(c.preview("t", list[0].id), /超出/);
  });
});
