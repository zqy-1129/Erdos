/**
 * SP3-6 合规声明导出测试（node:test）：
 * 留痕汇总可追溯、三格式快照内容一致、哈希脚注、人工说明必填、
 * 损坏降级不伪造、未用 AI 模板、空留痕不编造。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { exportDeclaration, ComplianceDataError } from "../declaration/export.ts";
import { summarizeTrail } from "../declaration/summary.ts";
import type { ArtifactEntry, TrailEvent, TrailSource } from "../declaration/types.ts";
import type { ComplianceExportResult, ComplianceSaveResult, RecentTask } from "../renderer/src/bridges/bridge.ts";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);

function makeTrail(): { events: TrailEvent[]; artifacts: ArtifactEntry[] } {
  const events: TrailEvent[] = [
    { id: 1, task_id: "t-1001", stage: "analysis", event_type: "model_call", detail: { model: "deepseek-chat", usage: { tokens: 120 }, duration_ms: 800 }, ts: "2026-10-01T08:00:00Z" },
    { id: 2, task_id: "t-1001", stage: "analysis", event_type: "model_call", detail: { model: "deepseek-chat", usage: { tokens: 90 }, duration_ms: 700 }, ts: "2026-10-01T08:01:00Z" },
    { id: 3, task_id: "t-1001", stage: "analysis", event_type: "tool_call", detail: { tool: "file.read", path: "data.csv" }, ts: "2026-10-01T08:01:30Z" },
    { id: 4, task_id: "t-1001", stage: "modeling", event_type: "model_call", detail: { model: "deepseek-reasoner", usage: { tokens: 300 }, duration_ms: 3000 }, ts: "2026-10-01T08:05:00Z" },
    { id: 5, task_id: "t-1001", stage: "modeling", event_type: "manual_edit", detail: { note: "修正目标函数约束" }, ts: "2026-10-01T08:10:00Z" },
    { id: 6, task_id: "t-1001", stage: "writing", event_type: "tool_call", detail: { tool: "plot.fig", path: null }, ts: "2026-10-01T08:20:00Z" },
  ];
  const artifacts: ArtifactEntry[] = [
    { task_id: "t-1001", stage: "writing", kind: "paper", file_path: "out/paper.md", sha256: SHA_A, size_bytes: 2048 },
    { task_id: "t-1001", stage: "solving", kind: "figure", file_path: "out/fig1.png", sha256: SHA_B, size_bytes: 51000 },
  ];
  return { events, artifacts };
}

class FakeSource implements TrailSource {
  private readonly data: { events: TrailEvent[]; artifacts: ArtifactEntry[] };

  constructor(data: { events: TrailEvent[]; artifacts: ArtifactEntry[] }) {
    this.data = data;
  }

  async events(): Promise<TrailEvent[]> {
    return this.data.events;
  }
  async artifacts(): Promise<ArtifactEntry[]> {
    return this.data.artifacts;
  }
}

class BrokenSource implements TrailSource {
  async events(): Promise<TrailEvent[]> {
    throw new Error("SQLITE_CORRUPT: database disk image is malformed");
  }
  async artifacts(): Promise<ArtifactEntry[]> {
    throw new Error("SQLITE_CORRUPT: database disk image is malformed");
  }
}

const BASE_OPTIONS = {
  taskId: "t-1001",
  humanNote: "数据预处理由本人手动完成；模型生成的图表经本人人工核验后使用。",
  unusedAi: false,
  generatedAt: "2026-10-03T10:00:00Z",
};

describe("留痕汇总（可追溯）", () => {
  it("工具清单去重（模型+工具）；参与度计数；哈希一一对应", () => {
    const { events, artifacts } = makeTrail();
    const data = summarizeTrail(events, artifacts, "t-1001");
    assert.deepEqual(data.toolList, ["deepseek-chat", "deepseek-reasoner", "file.read", "plot.fig"]);
    assert.equal(data.trailCount, 6);
    const analysis = data.stageParticipation.find((item) => item.stage === "analysis");
    assert.deepEqual(analysis, { stage: "analysis", modelCalls: 2, toolCalls: 1, manualEdits: 0 });
    const modeling = data.stageParticipation.find((item) => item.stage === "modeling");
    assert.deepEqual(modeling, { stage: "modeling", modelCalls: 1, toolCalls: 0, manualEdits: 1 });
    assert.deepEqual(
      data.artifactHashes.map((a) => a.sha256),
      [SHA_A, SHA_B],
    );
  });

  it("空留痕不编造：清单为空，任务标识保留", () => {
    const data = summarizeTrail([], [], "t-empty");
    assert.equal(data.taskId, "t-empty");
    assert.equal(data.toolList.length, 0);
    assert.equal(data.artifactHashes.length, 0);
    assert.equal(data.trailCount, 0);
  });
});

describe("三格式渲染与内容一致（快照）", () => {
  it("同一任务三格式导出，关键事实一致（工具/说明/哈希）", async () => {
    const source = new FakeSource(makeTrail());
    const md = await exportDeclaration(source, { ...BASE_OPTIONS, format: "md" });
    const latex = await exportDeclaration(source, { ...BASE_OPTIONS, format: "latex" });
    const docx = await exportDeclaration(source, { ...BASE_OPTIONS, format: "docx" });

    const mdText = md.content as string;
    const latexText = latex.content as string;
    const docxBuf = Buffer.from(docx.content as Uint8Array);

    // 内容一致：关键事实在三格式（文本格式）中一致呈现
    const facts = ["t-1001", "deepseek-chat", "人工修改说明", "数据预处理由本人手动完成", "a".repeat(16)];
    for (const fact of facts) {
      assert.ok(mdText.includes(fact), `md 缺字段：${fact}`);
      assert.ok(latexText.includes(fact), `latex 缺字段：${fact}`);
    }

    // docx：zip 魔数 + 非空体积（Word 可开由真实 Word/WPS 人工验收）
    assert.equal(docxBuf.subarray(0, 2).toString("latin1"), "PK");
    assert.ok(docxBuf.length > 1000);

    // 可追溯：产物哈希与留痕一一对应（无其它伪哈希来源）
    assert.ok(mdText.includes(SHA_A.slice(0, 16)));
    assert.ok(mdText.includes(SHA_B.slice(0, 16)));
    assert.ok(latexText.includes(SHA_A.slice(0, 16)));
    // docx 为压缩二进制：验证可生成即可（条目一致性在 md/latex 文本格式断言）
  });

  it("LaTeX 结构完整（可编译骨架）：documentclass 与 begin/end 平衡、footnote 引用哈希", async () => {
    const latex = await exportDeclaration(new FakeSource(makeTrail()), { ...BASE_OPTIONS, format: "latex" });
    const text = latex.content as string;
    assert.ok(text.startsWith("\\documentclass{article}"));
    assert.equal((text.match(/\\begin\{document\}/g) ?? []).length, 1);
    assert.equal((text.match(/\\end\{document\}/g) ?? []).length, 1);
    assert.ok(text.includes("\\footnote{paper 产物 sha256：" + SHA_A + "}"));
    assert.ok(text.includes("\\begin{tabular}") && text.includes("\\end{tabular}"));
  });

  it("Markdown 哈希脚注定义与引用成对", async () => {
    const md = await exportDeclaration(new FakeSource(makeTrail()), { ...BASE_OPTIONS, format: "md" });
    const text = md.content as string;
    assert.ok(text.includes(`[^1]: paper 产物 sha256：${SHA_A}`));
    assert.ok(text.includes(`[^2]: figure 产物 sha256：${SHA_B}`));
    assert.ok(text.includes(SHA_A.slice(0, 16) + "[^1]"));
  });
});

describe("必填与降级红线", () => {
  it("人工修改说明必填：空/空白拒导出", async () => {
    for (const note of ["", "   "]) {
      await assert.rejects(
        () => exportDeclaration(new FakeSource(makeTrail()), { ...BASE_OPTIONS, format: "md", humanNote: note }),
        (error: unknown): boolean =>
          error instanceof ComplianceDataError && error.message.includes("人工修改说明为必填项"),
      );
    }
  });

  it("留痕库损坏：降级提示重建且不产出声明（禁止伪造）", async () => {
    await assert.rejects(
      () => exportDeclaration(new BrokenSource(), { ...BASE_OPTIONS, format: "md" }),
      (error: unknown): boolean => {
        assert.ok(error instanceof ComplianceDataError);
        assert.ok(error.message.includes("留痕库不可用"));
        assert.ok(error.message.includes("重建"));
        assert.ok(error.message.includes("不会在留痕缺失时伪造条目"));
        return true;
      },
    );
  });

  it("未使用 AI 模板：无工具清单、无哈希，仅声明陈述（三格式）", async () => {
    const source = new FakeSource(makeTrail());
    for (const format of ["md", "latex", "docx"] as const) {
      const result = await exportDeclaration(source, { ...BASE_OPTIONS, format, unusedAi: true });
      if (format === "md") {
        const text = result.content as string;
        assert.ok(text.includes("未使用任何 AI 工具"));
        assert.ok(!text.includes("deepseek-chat"));
        assert.ok(!text.includes(SHA_A.slice(0, 16)));
      }
      if (format === "latex") {
        const text = result.content as string;
        assert.ok(text.includes("未使用任何 AI 工具"));
        assert.ok(!text.includes("deepseek-chat"));
        assert.ok(!text.includes("footnote"));
      }
      if (format === "docx") {
        assert.ok((result.content as Uint8Array).length > 500);
      }
    }
  });

  it("空留痕导出：模板显式标注无记录（不编造）", async () => {
    const md = await exportDeclaration(
      new FakeSource({ events: [], artifacts: [] }),
      { ...BASE_OPTIONS, format: "md" },
    );
    const text = md.content as string;
    assert.ok(text.includes("本任务留痕中无 AI 工具调用记录"));
    assert.ok(text.includes("本任务留痕中无产物登记记录"));
    assert.ok(!text.includes("gpt-")); // 不出现任何编造模型名
  });
});

describe("渲染层桥接入（web-bridge 合规通道）", () => {
  it("页面调用合规通道：真实模板生成 + 演示留痕数据闭环（任务号必填）", async () => {
    const { WebDemoBridge } = await import("../renderer/src/bridges/web-bridge.ts");
    const { BRIDGE_CHANNELS } = await import("../renderer/src/bridges/bridge.ts");
    const bridge = new WebDemoBridge();
    const result = await bridge.invoke<ComplianceExportResult>(BRIDGE_CHANNELS.complianceExport, {
      taskId: "demo-task",
      format: "md",
      unusedAi: false,
      humanNote: "图表由本人核验。",
    });
    assert.ok(result.content.includes("AI 工具使用声明"));
    assert.ok(result.content.includes("deepseek-chat"));
    assert.ok(result.filename.endsWith(".md"));
    assert.ok(result.filename.includes("demo-task"), "文件名应含任务号（与主进程桥同口径）");
    assert.deepEqual(result.artifactHashes, ["9f".repeat(32)]);

    // 未用 AI 模板走同一通道
    const unused = await bridge.invoke<ComplianceExportResult>(BRIDGE_CHANNELS.complianceExport, {
      taskId: "demo-task",
      format: "md",
      unusedAi: true,
      humanNote: "无 AI 使用。",
    });
    assert.ok(unused.content.includes("未使用任何 AI 工具"));
    assert.ok(!unused.content.includes("deepseek-chat"));
    assert.deepEqual(unused.artifactHashes, []); // 未使用 AI 声明不引用产物支撑材料

    // 人工说明缺失：桥抛可读必填错误（页面 ErrorState 消费）
    await assert.rejects(
      () =>
        bridge.invoke(BRIDGE_CHANNELS.complianceExport, {
          taskId: "demo-task",
          format: "md",
          unusedAi: false,
          humanNote: " ",
        }),
      /人工修改说明为必填项/,
    );

    // 任务号缺失：与主进程桥同口径拦截（演示桥不得掩盖空任务路径）
    await assert.rejects(
      () => bridge.invoke(BRIDGE_CHANNELS.complianceExport, { format: "md", unusedAi: false, humanNote: "说明" }),
      /暂无任务/,
    );

    // 格式白名单（共享校验）：不支持格式拒绝，与真实桥同口径（非静默降级）
    await assert.rejects(
      () => bridge.invoke(BRIDGE_CHANNELS.complianceExport, { taskId: "demo-task", format: "pdf", unusedAi: false, humanNote: "说明" }),
      /不支持的导出格式：pdf/,
    );
  });

  it("保存通道（演示不写盘）：三格式文件名后缀正确、savedPath 为 null；空任务/空说明/非法格式平价拦截", async () => {
    const { WebDemoBridge } = await import("../renderer/src/bridges/web-bridge.ts");
    const { BRIDGE_CHANNELS } = await import("../renderer/src/bridges/bridge.ts");
    const bridge = new WebDemoBridge();
    const base = { taskId: "demo-task", unusedAi: false, humanNote: "图表由本人核验。" };

    const md = await bridge.invoke<ComplianceSaveResult>(BRIDGE_CHANNELS.complianceSave, { ...base, format: "md" });
    assert.equal(md.filename, "AI工具使用声明_demo-task.md");
    assert.equal(md.savedPath, null, "演示模式不写盘：savedPath 为 null（与 billing:export 演示口径一致）");
    assert.equal(md.canceled, false);

    const latex = await bridge.invoke<ComplianceSaveResult>(BRIDGE_CHANNELS.complianceSave, { ...base, format: "latex" });
    assert.equal(latex.filename, "AI工具使用声明_demo-task.tex", "latex 扩展名映射 .tex");

    const docx = await bridge.invoke<ComplianceSaveResult>(BRIDGE_CHANNELS.complianceSave, { ...base, format: "docx" });
    assert.equal(docx.filename, "AI工具使用声明_demo-task.docx");

    // 门禁平价（与真实桥同文案）：任务号缺失、人工说明空、格式白名单
    await assert.rejects(
      () => bridge.invoke(BRIDGE_CHANNELS.complianceSave, { format: "md", unusedAi: false, humanNote: "说明" }),
      /暂无任务/,
    );
    await assert.rejects(
      () => bridge.invoke(BRIDGE_CHANNELS.complianceSave, { ...base, humanNote: " " }),
      /人工修改说明为必填项/,
    );
    await assert.rejects(
      () => bridge.invoke(BRIDGE_CHANNELS.complianceSave, { ...base, format: "pdf" }),
      /不支持的导出格式：pdf/,
    );
  });

  it("最近任务通道（演示）：返回演示留痕任务；空账号为空集", async () => {
    const { WebDemoBridge } = await import("../renderer/src/bridges/web-bridge.ts");
    const { BRIDGE_CHANNELS } = await import("../renderer/src/bridges/bridge.ts");
    const bridge = new WebDemoBridge();

    const tasks = await bridge.invoke<RecentTask[]>(BRIDGE_CHANNELS.trailRecentTasks);
    assert.deepEqual(tasks, [{ taskId: "demo-task", lastTs: "2026-10-02T08:20:00Z", eventCount: 4 }], "与演示留痕同源");

    const empty = await bridge.invoke<RecentTask[]>(BRIDGE_CHANNELS.trailRecentTasks, { username: "demo-empty" });
    assert.deepEqual(empty, [], "空账号演示：无留痕任务");
  });
});