/**
 * 三格式渲染（SP3-6）：Markdown / LaTeX / Word（docx）。
 *
 * - 字段只来自 DeclarationData（留痕）与用户提供的人工修改说明；
 * - 人工修改说明为必填项（导出层校验，渲染层不兜底）；
 * - 产物哈希以脚注形式引用（md 使用 [^n]、LaTeX 使用 \footnote、docx 使用
 *   文末附注段落，footnote 样式）；
 * - 未使用 AI 模板：不渲染工具清单与产物哈希，仅作声明陈述。
 */

import type { DeclarationData, DeclarationFormat } from "./types.ts";

export interface RenderOptions {
  data: DeclarationData;
  format: DeclarationFormat;
  /** 用户人工修改说明（必填，调用方校验非空）。 */
  humanNote: string;
  /** 未使用 AI 版本模板。 */
  unusedAi: boolean;
  generatedAt: string;
}

function escapeLatex(text: string): string {
  return text
    .replace(/\\/g, "\\textbackslash{}")
    .replace(/[&%$#_{}]/g, (ch) => `\\${ch}`)
    .replace(/\^/g, "\\^{}")
    .replace(/~/g, "\\~{}");
}

const TITLE = "AI 工具使用声明";

function toolSection(data: DeclarationData): string {
  if (data.toolList.length === 0) {
    return "本任务留痕中无 AI 工具调用记录。";
  }
  return data.toolList.join("、");
}

/** 脚注条目（产物哈希支撑材料）。 */
function hashFootnotes(data: DeclarationData): string[] {
  return data.artifactHashes.map(
    (artifact) => `${artifact.kind} 产物 sha256：${artifact.sha256}`,
  );
}

// ---------------------------------------------------------------------------
// Markdown
// ---------------------------------------------------------------------------

export function renderMarkdown(options: RenderOptions): string {
  const { data } = options;
  if (options.unusedAi) {
    return [
      `# ${TITLE}`,
      "",
      "本人声明：在本任务的全部环节中，未使用任何 AI 工具生成或辅助生成内容。",
      "",
      `- 任务编号：${data.taskId}`,
      `- 人工修改说明：${options.humanNote}`,
      `- 导出时间：${options.generatedAt}`,
      "",
    ].join("\n");
  }

  const table = [
    "| 阶段 | 模型调用 | 工具调用 | 人工修改 |",
    "| ---- | -------- | -------- | -------- |",
    ...data.stageParticipation.map(
      (item) => `| ${item.stage} | ${item.modelCalls} | ${item.toolCalls} | ${item.manualEdits} |`,
    ),
  ];

  const footnotes = hashFootnotes(data).map((note, i) => `[^${i + 1}]: ${note}`);

  return [
    `# ${TITLE}`,
    "",
    "本声明依据本机留痕库（audit_trail / artifact_index）自动生成，条目与留痕一一对应，未作任何虚构。",
    "",
    "## 基本信息",
    "",
    `- 任务编号：${data.taskId}`,
    `- 留痕事件数：${data.trailCount}`,
    `- 导出时间：${options.generatedAt}`,
    "",
    "## AI 工具清单",
    "",
    toolSection(data),
    "",
    "## 各环节 AI 参与度",
    "",
    ...table,
    "",
    "## 人工修改说明",
    "",
    options.humanNote,
    "",
    "## 产物哈希（支撑材料）",
    "",
    ...(data.artifactHashes.length > 0
      ? data.artifactHashes.map((artifact, i) => `${i + 1}. ${artifact.kind}：sha256 ${artifact.sha256}[^${i + 1}]`)
      : ["本任务留痕中无产物登记记录。"]),
    "",
    ...footnotes,
    "",
  ].join("\n");
}

// ---------------------------------------------------------------------------
// LaTeX（标准 article，可编译）
// ---------------------------------------------------------------------------

export function renderLatex(options: RenderOptions): string {
  const { data } = options;
  const body: string[] = [];

  if (options.unusedAi) {
    body.push(
      `本人声明：在本任务的全部环节中，未使用任何 AI 工具生成或辅助生成内容。`,
      "",
      `任务编号：${escapeLatex(data.taskId)}。`,
      `人工修改说明：${escapeLatex(options.humanNote)}。`,
      `导出时间：${escapeLatex(options.generatedAt)}。`,
    );
  } else {
    const rows = data.stageParticipation
      .map(
        (item) =>
          `  ${escapeLatex(item.stage)} & ${item.modelCalls} & ${item.toolCalls} & ${item.manualEdits} \\\\`,
      )
      .join("\n");
    body.push(
      "本声明依据本机留痕库（audit\\_trail / artifact\\_index）自动生成，条目与留痕一一对应，未作任何虚构。",
      "",
      `任务编号：${escapeLatex(data.taskId)}。`,
      `留痕事件数：${data.trailCount}。`,
      `导出时间：${escapeLatex(options.generatedAt)}。`,
      "",
      "\\subsection*{AI 工具清单}",
      escapeLatex(toolSection(data)) + "。",
      "",
      "\\subsection*{各环节 AI 参与度}",
      "\\begin{tabular}{lccc}",
      "  \\hline",
      "  阶段 & 模型调用 & 工具调用 & 人工修改 \\\\",
      "  \\hline",
      rows,
      "  \\hline",
      "\\end{tabular}",
      "",
      "\\subsection*{人工修改说明}",
      escapeLatex(options.humanNote),
    );
    for (const note of hashFootnotes(data)) {
      body.push("", "\\footnote{" + escapeLatex(note) + "}");
    }
  }

  return [
    "\\documentclass{article}",
    "\\usepackage[UTF8]{ctex}",
    "\\title{AI 工具使用声明}",
    "\\author{}",
    "\\date{}",
    "\\begin{document}",
    "\\maketitle",
    "",
    ...body,
    "",
    "\\end{document}",
    "",
  ].join("\n");
}

// ---------------------------------------------------------------------------
// Word（docx，动态加载 docx 包以隔离体积）
// ---------------------------------------------------------------------------

/** 解析 docx 包为 Buffer（浏览器/Node 双端可用）。 */
export async function renderDocx(options: RenderOptions): Promise<Uint8Array> {
  const docx = await import("docx");
  const { data } = options;
  const children: Array<InstanceType<typeof docx.Paragraph> | InstanceType<typeof docx.Table>> = [];

  const p = (text: string, opts?: { heading?: boolean; note?: boolean; bold?: boolean }) =>
    new docx.Paragraph({
      heading: opts?.heading ? docx.HeadingLevel.HEADING_1 : undefined,
      style: opts?.note ? "FootnoteText" : undefined,
      children: [new docx.TextRun({ text, bold: opts?.bold })],
    });

  children.push(p(TITLE, { heading: true, bold: true }));

  if (options.unusedAi) {
    children.push(
      p("本人声明：在本任务的全部环节中，未使用任何 AI 工具生成或辅助生成内容。"),
      p(`任务编号：${data.taskId}`),
      p(`人工修改说明：${options.humanNote}`),
      p(`导出时间：${options.generatedAt}`),
    );
  } else {
    children.push(
      p("本声明依据本机留痕库（audit_trail / artifact_index）自动生成，条目与留痕一一对应，未作任何虚构。"),
      p(`任务编号：${data.taskId}`),
      p(`留痕事件数：${data.trailCount}`),
      p(`导出时间：${options.generatedAt}`),
      p("AI 工具清单：", { bold: true }),
      p(toolSection(data)),
      p("各环节 AI 参与度", { heading: true }),
    );
    const rows = [
      new docx.TableRow({
        children: ["阶段", "模型调用", "工具调用", "人工修改"].map(
          (head) => new docx.TableCell({ children: [p(head, { bold: true })] }),
        ),
      }),
      ...data.stageParticipation.map(
        (item) =>
          new docx.TableRow({
            children: [item.stage, String(item.modelCalls), String(item.toolCalls), String(item.manualEdits)].map(
              (cell) => new docx.TableCell({ children: [p(cell)] }),
            ),
          }),
      ),
    ];
    children.push(new docx.Table({ rows, width: { size: 100, type: docx.WidthType.PERCENTAGE } }));
    children.push(p("人工修改说明", { heading: true }), p(options.humanNote));
    // 产物哈希附注（脚注样式段落，文末引用）
    if (data.artifactHashes.length > 0) {
      children.push(p("附注（产物哈希支撑材料）", { bold: true }));
      for (const note of hashFootnotes(data)) {
        children.push(p(note, { note: true }));
      }
    } else {
      children.push(p("本任务留痕中无产物登记记录。"));
    }
  }

  const document = new docx.Document({
    creator: "Erdos Client",
    sections: [{ children }],
  });
  // 双端兼容：toBlob（浏览器/Node 均可），避免 toBuffer 在浏览器缺 Buffer 实现
  const blob = await docx.Packer.toBlob(document);
  return new Uint8Array(await blob.arrayBuffer());
}