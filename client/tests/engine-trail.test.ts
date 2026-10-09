/**
 * 引擎留痕读取测试（F-002 用量估算 + SP3-6 声明真实数据源）：
 * 读取器形状忠实/异常语义（缺失区分「无记录」与「读取失败」）、声明与用量集成，
 * 以及真实引擎进程（FakeLLM）留痕落库 → 客户端读取的跨进程集成。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

import {
  complianceExportViewFromEngineTrail,
  complianceSaveFromEngineTrail,
  createEngineTrailSource,
  exportDeclarationFromEngineTrail,
  readRecentTasks,
  readUsageEvents,
} from "../main/engine-trail.ts";
import type { DeclarationFileSaver } from "../main/engine-trail.ts";
import { ComplianceDataError } from "../declaration/export.ts";
import { estimateUsage } from "../shared/usage.ts";
import { EngineHost } from "../main/engine-host/host.ts";
import type { EngineEvent } from "../shared/ipc.ts";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

// ---------------------------------------------------------------------------
// 测试资产：按引擎 TrailStore 的建表语句构造临时 audit.db
// ---------------------------------------------------------------------------

interface SeedEvent {
  task_id: string;
  stage: string;
  event_type: string;
  detail: unknown;
  ts: string;
}

interface SeedArtifact {
  task_id: string;
  stage: string;
  kind: string;
  file_path: string;
  sha256: string;
  size_bytes: number;
}

function createTrailDb(options: { events?: SeedEvent[]; artifacts?: SeedArtifact[] } = {}): {
  dir: string;
  dbPath: string;
} {
  const dir = mkdtempSync(join(tmpdir(), "erdos-trail-"));
  const dbPath = join(dir, "audit.db");
  const db = new DatabaseSync(dbPath);
  // 与 engine/trail/store.py 建表语句逐字一致（跨端 schema 约定）
  db.exec(`CREATE TABLE IF NOT EXISTS audit_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    ts TEXT NOT NULL
  )`);
  db.exec(`CREATE TABLE IF NOT EXISTS artifact_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
  )`);
  for (const event of options.events ?? []) {
    // 字符串 detail 按原始文本落库（用于构造非法 JSON 场景）；对象按引擎口径 JSON 序列化
    const detailText = typeof event.detail === "string" ? event.detail : JSON.stringify(event.detail);
    db.prepare("INSERT INTO audit_trail (task_id, stage, event_type, detail, ts) VALUES (?, ?, ?, ?, ?)").run(
      event.task_id,
      event.stage,
      event.event_type,
      detailText,
      event.ts,
    );
  }
  for (const artifact of options.artifacts ?? []) {
    db.prepare(
      "INSERT INTO artifact_index (task_id, stage, kind, file_path, sha256, size_bytes, created_at) " +
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
    ).run(
      artifact.task_id,
      artifact.stage,
      artifact.kind,
      artifact.file_path,
      artifact.sha256,
      artifact.size_bytes,
      "2026-10-08T00:00:00+00:00",
    );
  }
  db.close();
  return { dir, dbPath };
}

const HEX64 = "a".repeat(64);

function seedEvents(): SeedEvent[] {
  return [
    {
      task_id: "t-1",
      stage: "analysis",
      event_type: "model_call",
      detail: { model: "deepseek-chat", usage: { prompt_tokens: 100, completion_tokens: 50 } },
      ts: "2026-10-08T01:00:00+00:00",
    },
    {
      task_id: "t-1",
      stage: "analysis",
      event_type: "tool_call",
      detail: { tool: "run_python", ok: true },
      ts: "2026-10-08T01:01:00+00:00",
    },
    {
      task_id: "t-1",
      stage: "modeling",
      event_type: "model_call",
      detail: { model: "unknown-model", usage: { total_tokens: 30 } },
      ts: "2026-10-08T01:02:00+00:00",
    },
    {
      task_id: "t-1",
      stage: "modeling",
      event_type: "manual_edit",
      detail: { note: "人工修订公式" },
      ts: "2026-10-08T01:03:00+00:00",
    },
    {
      task_id: "t-2",
      stage: "analysis",
      event_type: "model_call",
      detail: { model: "deepseek-reasoner", usage: { input_tokens: 10, output_tokens: 5 } },
      ts: "2026-10-08T02:00:00+00:00",
    },
  ];
}

// ---------------------------------------------------------------------------
// 读取器：形状忠实与异常语义
// ---------------------------------------------------------------------------

describe("引擎留痕读取器", () => {
  it("事件按追加序返回且字段忠实（detail JSON 解析）；产物索引独立读取", async () => {
    const { dir, dbPath } = createTrailDb({
      events: seedEvents(),
      artifacts: [
        { task_id: "t-1", stage: "writing", kind: "paper", file_path: "tasks/t-1/paper.md", sha256: HEX64, size_bytes: 843 },
      ],
    });
    try {
      const source = createEngineTrailSource(dbPath);
      const events = await source.events("t-1");
      assert.deepEqual(events.map((e) => e.event_type), ["model_call", "tool_call", "model_call", "manual_edit"]);
      assert.equal(events[0]?.id, 1, "应保留引擎自增 id");
      assert.deepEqual(events[0]?.detail, {
        model: "deepseek-chat",
        usage: { prompt_tokens: 100, completion_tokens: 50 },
      });
      assert.equal(events[0]?.ts, "2026-10-08T01:00:00+00:00");

      const artifacts = await source.artifacts("t-1");
      assert.deepEqual(artifacts, [
        { task_id: "t-1", stage: "writing", kind: "paper", file_path: "tasks/t-1/paper.md", sha256: HEX64, size_bytes: 843 },
      ]);
      assert.deepEqual(await source.artifacts("t-unknown"), [], "未知任务为空列表（非错误）");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("事件类型漂移（未知类型）→ 抛可读降级（版本不兼容，不误导为库损坏/漏计）", async () => {
    const { dir, dbPath } = createTrailDb({
      events: [{ task_id: "t-1", stage: "analysis", event_type: "future_event", detail: {}, ts: "t" }],
    });
    try {
      await assert.rejects(
        () => createEngineTrailSource(dbPath).events("t-1"),
        (error: unknown) =>
          error instanceof ComplianceDataError && /留痕事件类型未知（future_event）/.test(error.message),
      );
      // 经声明导出不应被替换为「库损坏可重建」的通用提示（保留具体归因）
      await assert.rejects(
        () =>
          exportDeclarationFromEngineTrail(dbPath, { taskId: "t-1", format: "md", humanNote: "说明", unusedAi: false }),
        (error: unknown) =>
          error instanceof ComplianceDataError &&
          /不兼容版本/.test(error.message) &&
          !/重建留痕库/.test(error.message),
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("detail 容错：非对象（null/数组）按空对象处理；非法 JSON → 可读降级（不崩溃）", async () => {
    const { dir, dbPath } = createTrailDb({
      events: [
        { task_id: "t-1", stage: "analysis", event_type: "model_call", detail: null, ts: "t1" },
        { task_id: "t-1", stage: "modeling", event_type: "tool_call", detail: [1, 2], ts: "t2" },
      ],
    });
    try {
      const events = await createEngineTrailSource(dbPath).events("t-1");
      assert.deepEqual(events.map((event) => event.detail), [{}, {}], "非对象 detail 按空对象容错");
      // 用量估算对空 detail 安全（计一次调用、0 token、不臆测费用）
      const estimate = estimateUsage(events.map((event) => ({ event_type: event.event_type, detail: event.detail })));
      assert.equal(estimate.modelCalls, 1);
      assert.equal(estimate.totalTokens, 0);
      assert.equal(estimate.estimatedCostCents, null);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }

    const broken = createTrailDb({
      events: [{ task_id: "t-1", stage: "analysis", event_type: "model_call", detail: "{not-json", ts: "t" }],
    });
    try {
      await assert.rejects(
        () => createEngineTrailSource(broken.dbPath).events("t-1"),
        (error: unknown) => error instanceof ComplianceDataError && /留痕记录解析失败/.test(error.message),
      );
      assert.throws(() => readUsageEvents(broken.dbPath), /本地留痕库读取失败/);
    } finally {
      rmSync(broken.dir, { recursive: true, force: true });
    }
  });

  it("库缺失语义区分：声明源抛错（合规降级）；用量源返回空集（引擎未运行→无记录）", async () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-trail-"));
    try {
      const missing = join(dir, "audit.db");
      await assert.rejects(
        () => createEngineTrailSource(missing).events("t-1"),
        /unable to open database file|ENOENT/,
        "声明读取不允许在留痕缺失时静默返回空",
      );
      assert.deepEqual(readUsageEvents(missing), [], "用量源：库不存在=无记录（0 是事实）");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("库损坏（非 SQLite 文件）→ 抛错（不把未知冒充为 0）", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-trail-"));
    try {
      const dbPath = join(dir, "audit.db");
      writeFileSync(dbPath, "not a sqlite database", "utf-8");
      assert.throws(() => readUsageEvents(dbPath), /file is not a database|SQLITE|malformed/i);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 声明导出（真实留痕源）
// ---------------------------------------------------------------------------

describe("声明导出（引擎留痕数据源）", () => {
  it("按任务生成声明：工具清单/阶段参与度/产物哈希/事件总数与留痕一致", async () => {
    const { dir, dbPath } = createTrailDb({
      events: seedEvents(),
      artifacts: [
        { task_id: "t-1", stage: "writing", kind: "paper", file_path: "paper.md", sha256: HEX64, size_bytes: 843 },
      ],
    });
    try {
      const exported = await exportDeclarationFromEngineTrail(dbPath, {
        taskId: "t-1",
        format: "md",
        humanNote: "数据预处理由本人手动完成。",
        unusedAi: false,
        generatedAt: "2026-10-08T03:00:00+00:00",
      });
      assert.equal(exported.filename, "AI工具使用声明_t-1.md");
      assert.equal(exported.data.trailCount, 4);
      assert.deepEqual(exported.data.artifactHashes, [{ kind: "paper", sha256: HEX64 }]);
      assert.ok(exported.data.toolList.includes("deepseek-chat"), `工具清单应含模型：${JSON.stringify(exported.data.toolList)}`);
      assert.ok(exported.data.toolList.includes("run_python"), "工具清单应含工具调用");
      const analysis = exported.data.stageParticipation.find((stage) => stage.stage === "analysis");
      assert.deepEqual(analysis, { stage: "analysis", modelCalls: 1, toolCalls: 1, manualEdits: 0 });
      assert.ok(String(exported.content).includes("数据预处理由本人手动完成。"), "声明应含人工修改说明");
      assert.ok(String(exported.content).includes(HEX64), "声明应含产物 sha256 支撑材料");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("空任务号 → 可读报错；不支持的格式 → 可读报错；人工说明空 → 必填报错", async () => {
    const { dir, dbPath } = createTrailDb();
    try {
      await assert.rejects(
        () => exportDeclarationFromEngineTrail(dbPath, { taskId: "  ", format: "md", humanNote: "说明", unusedAi: false }),
        /暂无任务/,
      );
      await assert.rejects(
        () =>
          exportDeclarationFromEngineTrail(dbPath, {
            taskId: "t-1",
            format: "pdf" as never,
            humanNote: "说明",
            unusedAi: false,
          }),
        /不支持的导出格式：pdf/,
      );
      await assert.rejects(
        () => exportDeclarationFromEngineTrail(dbPath, { taskId: "t-1", format: "md", humanNote: "  ", unusedAi: false }),
        /人工修改说明为必填项/,
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("留痕库缺失 → ComplianceDataError（降级提示重建，绝不伪造）", async () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-trail-"));
    try {
      await assert.rejects(
        () =>
          exportDeclarationFromEngineTrail(join(dir, "audit.db"), {
            taskId: "t-1",
            format: "md",
            humanNote: "说明",
            unusedAi: false,
          }),
        (error: unknown) =>
          error instanceof ComplianceDataError && /本地留痕库不可用/.test(error.message),
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("桥视图映射：格式白名单 / docx 预览策略与文件名 / 未使用 AI 不回传产物哈希", async () => {
    const { dir, dbPath } = createTrailDb({
      events: seedEvents(),
      artifacts: [
        { task_id: "t-1", stage: "writing", kind: "paper", file_path: "paper.md", sha256: HEX64, size_bytes: 843 },
      ],
    });
    try {
      const base = { taskId: "t-1", humanNote: "说明", unusedAi: false };

      await assert.rejects(
        () => complianceExportViewFromEngineTrail(dbPath, { ...base, format: "pdf" }),
        /不支持的导出格式：pdf/,
      );
      await assert.rejects(
        () =>
          complianceExportViewFromEngineTrail(dbPath, {
            taskId: "  ",
            format: "md",
            humanNote: "说明",
            unusedAi: false,
          }),
        /暂无任务/,
      );

      const md = await complianceExportViewFromEngineTrail(dbPath, { ...base, format: "md" });
      assert.equal(md.filename, "AI工具使用声明_t-1.md");
      assert.deepEqual(md.artifactHashes, [HEX64], "真实留痕的产物哈希应回传（US-007 支撑材料）");

      const docx = await complianceExportViewFromEngineTrail(dbPath, { ...base, format: "docx" });
      assert.equal(docx.filename, "AI工具使用声明_t-1.docx", "docx 文件名换后缀");
      assert.ok(docx.content.includes("本页仅以同源 Markdown 预览"), "docx 应明确标注为预览而非二进制正文");
      assert.ok(docx.content.includes("保存文件将生成真正的 .docx 文档"), "docx 应指引保存可获得真实二进制文档");
      assert.ok(docx.content.includes("deepseek-chat"), "docx 预览为同源 Markdown 渲染（含工具清单）");

      const unused = await complianceExportViewFromEngineTrail(dbPath, { ...base, format: "md", unusedAi: true });
      assert.deepEqual(unused.artifactHashes, [], "未使用 AI 声明不引用产物支撑材料");
      assert.ok(unused.content.includes("未使用"), "未使用 AI 版本正文模板");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 声明文件保存（SP3-6 收尾第二批：md/latex 文本 + docx 真实二进制落盘）
// ---------------------------------------------------------------------------

describe("声明文件保存（三格式真实落盘）", () => {
  it("docx：落盘内容为真实 zip 二进制（PK 魔数）；滤镜/建议名/回显路径取自保存结果", async () => {
    const { dir, dbPath } = createTrailDb({
      events: seedEvents(),
      artifacts: [
        { task_id: "t-1", stage: "writing", kind: "paper", file_path: "paper.md", sha256: HEX64, size_bytes: 843 },
      ],
    });
    try {
      const requests: Array<Parameters<DeclarationFileSaver>[0]> = [];
      const save: DeclarationFileSaver = async (request) => {
        requests.push(request);
        return { canceled: false, path: "/tmp/AI工具使用声明_t-1.docx" };
      };
      const view = await complianceSaveFromEngineTrail(
        dbPath,
        { taskId: "t-1", format: "docx", humanNote: "图表经本人核验。", unusedAi: false },
        save,
      );
      assert.equal(requests.length, 1, "保存器应恰好被调用一次");
      const request = requests[0];
      assert.equal(request.suggestedName, "AI工具使用声明_t-1.docx");
      assert.equal(request.filterName, "Word 文档");
      assert.deepEqual(request.extensions, ["docx"]);
      assert.ok(request.content instanceof Uint8Array, "docx 落盘内容应为二进制（非文本预览占位）");
      assert.equal(
        Buffer.from(request.content as Uint8Array).subarray(0, 2).toString("latin1"),
        "PK",
        "docx 应为真实 zip 文档（Word 可开由人工验收）",
      );
      assert.equal(view.savedPath, "/tmp/AI工具使用声明_t-1.docx");
      assert.equal(view.filename, "AI工具使用声明_t-1.docx", "回显文件名取自实际保存路径");
      assert.equal(view.canceled, false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("md/latex：文本内容含留痕事实；滤镜与扩展名正确；保存器无路径时回显建议名", async () => {
    const { dir, dbPath } = createTrailDb({ events: seedEvents() });
    try {
      const captured: Array<Parameters<DeclarationFileSaver>[0]> = [];
      const save: DeclarationFileSaver = async (request) => {
        captured.push(request);
        return { canceled: false, path: null };
      };
      const mdView = await complianceSaveFromEngineTrail(
        dbPath,
        { taskId: "t-1", format: "md", humanNote: "数据预处理由本人手动完成。", unusedAi: false },
        save,
      );
      const mdRequest = captured[0];
      assert.equal(mdRequest.suggestedName, "AI工具使用声明_t-1.md");
      assert.equal(mdRequest.filterName, "Markdown 文件");
      assert.deepEqual(mdRequest.extensions, ["md"]);
      assert.equal(typeof mdRequest.content, "string", "md 为文本落盘");
      assert.ok((mdRequest.content as string).includes("deepseek-chat"), "md 文本应含留痕工具清单");
      assert.ok((mdRequest.content as string).includes("数据预处理由本人手动完成。"), "md 文本应含人工修改说明");
      assert.equal(mdView.filename, "AI工具使用声明_t-1.md", "保存器未给路径时回显建议文件名");
      assert.equal(mdView.savedPath, null);

      const latexView = await complianceSaveFromEngineTrail(
        dbPath,
        { taskId: "t-1", format: "latex", humanNote: "说明", unusedAi: false },
        save,
      );
      const latexRequest = captured[1];
      assert.equal(latexRequest.suggestedName, "AI工具使用声明_t-1.tex", "latex 扩展名映射为 .tex");
      assert.equal(latexRequest.filterName, "LaTeX 文件");
      assert.deepEqual(latexRequest.extensions, ["tex"]);
      assert.ok((latexRequest.content as string).startsWith("\\documentclass{article}"));
      assert.equal(latexView.savedPath, null);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("取消 → 静默（canceled 且回显建议名）；写盘失败归一可读；门禁未过不触保存器", async () => {
    const { dir, dbPath } = createTrailDb({ events: seedEvents() });
    try {
      const cancelView = await complianceSaveFromEngineTrail(
        dbPath,
        { taskId: "t-1", format: "md", humanNote: "说明", unusedAi: false },
        async () => ({ canceled: true, path: null }),
      );
      assert.equal(cancelView.canceled, true);
      assert.equal(cancelView.savedPath, null);
      assert.equal(cancelView.filename, "AI工具使用声明_t-1.md", "取消时回显建议文件名");

      await assert.rejects(
        () =>
          complianceSaveFromEngineTrail(
            dbPath,
            { taskId: "t-1", format: "md", humanNote: "说明", unusedAi: false },
            async () => {
              throw new Error("EACCES: permission denied, open '/root/x.md'");
            },
          ),
        (error: unknown) =>
          error instanceof Error && /^导出保存失败：EACCES/.test(error.message) && error.cause instanceof Error,
      );

      // 门禁先于保存：非法格式/空任务/空说明均不触保存器（fail-closed，避免误写文件）
      let saveCalls = 0;
      const countingSave: DeclarationFileSaver = async () => {
        saveCalls += 1;
        return { canceled: false, path: "/tmp/x.md" };
      };
      await assert.rejects(
        () =>
          complianceSaveFromEngineTrail(
            dbPath,
            { taskId: "t-1", format: "pdf", humanNote: "说明", unusedAi: false },
            countingSave,
          ),
        /不支持的导出格式：pdf/,
      );
      await assert.rejects(
        () =>
          complianceSaveFromEngineTrail(
            dbPath,
            { taskId: "  ", format: "md", humanNote: "说明", unusedAi: false },
            countingSave,
          ),
        /暂无任务/,
      );
      await assert.rejects(
        () =>
          complianceSaveFromEngineTrail(
            dbPath,
            { taskId: "t-1", format: "md", humanNote: "  ", unusedAi: false },
            countingSave,
          ),
        /人工修改说明为必填项/,
      );
      assert.equal(saveCalls, 0, "任何门禁未过都不得触发保存器（不写盘）");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 最近任务（合规声明任务来源）
// ---------------------------------------------------------------------------

describe("最近任务（合规声明任务来源）", () => {
  it("按最后留痕时间倒序；计数正确；空 task_id 行剔除", () => {
    const { dir, dbPath } = createTrailDb({
      events: [
        { task_id: "t-old", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-07T01:00:00+00:00" },
        { task_id: "t-new", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-08T01:00:00+00:00" },
        { task_id: "t-new", stage: "modeling", event_type: "tool_call", detail: {}, ts: "2026-10-08T02:00:00+00:00" },
        // 引擎侧观察项：空 task_id 留痕行不可导出，不应出现在任务来源
        { task_id: "", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-09T00:00:00+00:00" },
      ],
    });
    try {
      assert.deepEqual(readRecentTasks(dbPath), [
        { taskId: "t-new", lastTs: "2026-10-08T02:00:00+00:00", eventCount: 2 },
        { taskId: "t-old", lastTs: "2026-10-07T01:00:00+00:00", eventCount: 1 },
      ]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("库缺失 → 空集（无记录是事实）；空表 → 空集", () => {
    const missingDir = mkdtempSync(join(tmpdir(), "erdos-trail-"));
    const { dir, dbPath } = createTrailDb();
    try {
      assert.deepEqual(readRecentTasks(join(missingDir, "audit.db")), [], "库不存在=无记录");
      assert.deepEqual(readRecentTasks(dbPath), [], "空表=无任务");
    } finally {
      rmSync(missingDir, { recursive: true, force: true });
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("limit 截断：仅返回最近 N 个任务", () => {
    const { dir, dbPath } = createTrailDb({
      events: [
        { task_id: "t-a", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-05T00:00:00+00:00" },
        { task_id: "t-b", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-06T00:00:00+00:00" },
        { task_id: "t-c", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-07T00:00:00+00:00" },
      ],
    });
    try {
      assert.deepEqual(
        readRecentTasks(dbPath, 2).map((task) => task.taskId),
        ["t-c", "t-b"],
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("并列时间：按 task_id 升序确定（顺序稳定，不抖动）", () => {
    const { dir, dbPath } = createTrailDb({
      events: [
        { task_id: "t-b", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-08T01:00:00+00:00" },
        { task_id: "t-a", stage: "analysis", event_type: "model_call", detail: {}, ts: "2026-10-08T01:00:00+00:00" },
      ],
    });
    try {
      assert.deepEqual(
        readRecentTasks(dbPath).map((task) => task.taskId),
        ["t-a", "t-b"],
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("库损坏 → 抛可读错误（不把未知冒充为无任务）", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-trail-"));
    try {
      const dbPath = join(dir, "audit.db");
      writeFileSync(dbPath, "not a sqlite database", "utf-8");
      assert.throws(() => readRecentTasks(dbPath), /本地留痕库读取失败/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 用量估算（F-002）数据源
// ---------------------------------------------------------------------------

describe("用量估算（引擎留痕数据源）", () => {
  it("仅统计 model_call；已知单价模型计价、未知模型不估算（estimatedCostCents=null）", () => {
    const { dir, dbPath } = createTrailDb({ events: seedEvents() });
    try {
      const events = readUsageEvents(dbPath);
      assert.equal(events.length, 3, "只取 model_call（tool_call/manual_edit 不计）");
      assert.ok(
        events.every((event) => event.event_type === "model_call"),
        "事件类型应始终为 model_call",
      );

      const estimate = estimateUsage(events);
      assert.equal(estimate.modelCalls, 3);
      assert.deepEqual(estimate.models.sort(), ["deepseek-chat", "deepseek-reasoner", "unknown-model"]);
      // deepseek-chat：100 输入×0.2 + 50 输出×0.8 = 60 分；reasoner：10×0.4 + 5×1.6 = 12 分
      assert.equal(estimate.totalTokens, 150 + 30 + 15);
      assert.equal(estimate.ratedCalls, 2, "未知模型不参与计价");
      assert.equal(estimate.estimatedCostCents, null, "存在未定价模型时不给出误导性总额");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("全为已知单价模型 → 给出费用估算（分）", () => {
    const { dir, dbPath } = createTrailDb({
      events: [
        {
          task_id: "t-1",
          stage: "solving",
          event_type: "model_call",
          detail: { model: "deepseek-chat", usage: { prompt_tokens: 1000, completion_tokens: 500 } },
          ts: "t",
        },
      ],
    });
    try {
      const estimate = estimateUsage(readUsageEvents(dbPath));
      assert.equal(estimate.modelCalls, 1);
      assert.equal(estimate.ratedCalls, 1);
      // 1000×0.2 + 500×0.8 = 600 分 / 1000 tokens 单价 = 0.6 分
      assert.equal(estimate.estimatedCostCents, 0.6);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// 真实引擎集成（跨进程：引擎落留痕 → 客户端读取）
// ---------------------------------------------------------------------------

/** 解析引擎可执行（与 engine-host.test.ts 同口径）：无则 skip。 */
function resolveEnginePython(): string | null {
  if (process.env.ERDOS_ENGINE_PYTHON) return process.env.ERDOS_ENGINE_PYTHON;
  const candidates = [
    join(repoRoot, "engine", ".venv", "Scripts", "python.exe"),
    join(repoRoot, "engine", ".venv", "bin", "python"),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

const enginePython = resolveEnginePython();

describe("真实引擎留痕集成（FakeLLM）", { skip: enginePython === null }, () => {
  it("分析阶段完成后：引擎 audit.db 可被客户端读取（model_call + 用量估算）", async () => {
    const home = mkdtempSync(join(tmpdir(), "erdos-trail-e2e-"));
    const events: EngineEvent[] = [];
    const host = new EngineHost({
      command: enginePython!,
      args: ["-m", "engine"],
      cwd: repoRoot,
      home,
      key: () => null, // FakeLLM 无 Key 模式
      onEvent: (event) => events.push(event as EngineEvent),
      onStateChange: () => {},
      onLog: () => {},
      onProtocolError: () => {},
    });
    try {
      await host.invoke("task_create", { task_id: "trail-task", title: "留痕集成", problem_text: "题面" });
      await host.invoke("start_stage", { task_id: "trail-task", stage: "analysis" });

      // 等待阶段完成事件（progress≥1）；留痕行在阶段产出时写入
      const deadline = Date.now() + 30_000;
      while (
        !events.some((event) => event.event === "stage.progress" && event.progress >= 1) &&
        Date.now() < deadline
      ) {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      assert.ok(
        events.some((event) => event.event === "stage.progress" && event.progress >= 1),
        "分析阶段应在超时前完成（进度事件）",
      );

      const dbPath = join(home, "audit.db");
      assert.ok(existsSync(dbPath), "引擎应在 home 下创建 audit.db");

      // 跨进程读取：引擎自持写连接，客户端只读连接可直接读取
      const trailEvents = await createEngineTrailSource(dbPath).events("trail-task");
      const modelCalls = trailEvents.filter((event) => event.event_type === "model_call");
      assert.ok(modelCalls.length >= 1, `分析阶段应至少记录一次 model_call：${JSON.stringify(trailEvents)}`);
      const estimate = estimateUsage(readUsageEvents(dbPath));
      assert.ok(estimate.modelCalls >= 1, "用量估算应统计到引擎写入的调用");
      assert.ok(estimate.totalTokens > 0, `FakeLLM 应上报 token usage：${JSON.stringify(estimate)}`);
    } finally {
      host.stop();
      // 等待引擎进程退出后再清理（引擎持有 audit.db 写连接，未退出时目录删除会 EBUSY）
      const exitDeadline = Date.now() + 5000;
      while (host.childPid !== null && Date.now() < exitDeadline) {
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      rmSync(home, { recursive: true, force: true });
    }
  });
});