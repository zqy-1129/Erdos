/**
 * 本地历史数据源与续跑定位测试（US-003 客户端侧）：
 * - 单元：留痕 × 检查点组合视图（标题/状态/可续）、恢复定位矩阵、库缺失/损坏口径；
 * - 集成：真实引擎（FakeLLM）产出检查点 → 重启后客户端定位下一阶段并经既有 RPC 续跑
 *   （零引擎改动；题面由引擎侧检查点水合，无需补投）。
 */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { EngineHost } from "../main/engine-host/host.ts";
import { deriveResumeStage, readLocalHistory, resolveResumeStage } from "../main/local-history.ts";
import type { EngineEvent, StageName } from "../shared/ipc.ts";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** 建留痕库（最小列集：读取器仅用 task_id/ts）。 */
function createTrailDb(dbPath: string, rows: Array<{ taskId: string; ts: string }>): void {
  const db = new DatabaseSync(dbPath);
  try {
    db.exec("CREATE TABLE audit_trail (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts TEXT NOT NULL)");
    const insert = db.prepare("INSERT INTO audit_trail (task_id, ts) VALUES (?, ?)");
    for (const row of rows) insert.run(row.taskId, row.ts);
  } finally {
    db.close();
  }
}

/** 建检查点库（对齐引擎 schema：checkpoints + task_inputs）。 */
function createCheckpointsDb(
  dbPath: string,
  titles: Array<{ taskId: string; title: string }>,
  checkpoints: Array<{ taskId: string; stage: string; status: string }>,
): void {
  const db = new DatabaseSync(dbPath);
  try {
    db.exec(
      "CREATE TABLE checkpoints (task_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL," +
        " step INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL," +
        " PRIMARY KEY (task_id, stage))",
    );
    db.exec(
      "CREATE TABLE task_inputs (task_id TEXT PRIMARY KEY, title TEXT NOT NULL," +
        " problem_text TEXT NOT NULL, updated_at TEXT NOT NULL)",
    );
    const insertTitle = db.prepare(
      "INSERT INTO task_inputs (task_id, title, problem_text, updated_at) VALUES (?, ?, '', '2026-10-09T00:00:00Z')",
    );
    for (const title of titles) insertTitle.run(title.taskId, title.title);
    const insertCheckpoint = db.prepare(
      "INSERT INTO checkpoints (task_id, stage, status, step, data, updated_at) VALUES (?, ?, ?, 0, '{}', '2026-10-09T00:00:00Z')",
    );
    for (const checkpoint of checkpoints) insertCheckpoint.run(checkpoint.taskId, checkpoint.stage, checkpoint.status);
  } finally {
    db.close();
  }
}

describe("本地历史数据源（单元）", () => {
  it("列表：留痕时间倒序 × 检查点状态派生（title/status/resumable/updatedAt）", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-history-unit-"));
    try {
      const trailDb = join(dir, "audit.db");
      const checkpointsDb = join(dir, "checkpoints.db");
      createTrailDb(trailDb, [
        { taskId: "t-created", ts: "2026-10-09T01:00:00Z" },
        { taskId: "t-partial", ts: "2026-10-09T02:00:00Z" },
        { taskId: "t-done", ts: "2026-10-09T03:00:00Z" },
      ]);
      createCheckpointsDb(
        checkpointsDb,
        [
          { taskId: "t-done", title: "已完成题" },
          { taskId: "t-partial", title: "续跑题" },
        ],
        [
          ...(["analysis", "modeling", "solving", "writing"] as const).map((stage) => ({
            taskId: "t-done",
            stage,
            status: "done",
          })),
          { taskId: "t-partial", stage: "analysis", status: "done" },
        ],
      );

      const list = readLocalHistory(trailDb, checkpointsDb);
      assert.deepEqual(
        list.map((task) => task.taskId),
        ["t-done", "t-partial", "t-created"],
        "按最后留痕时间倒序",
      );
      assert.deepEqual(
        list.map((task) => task.status),
        ["done", "modeling", "created"],
        "状态=下一待完成阶段；全过门禁=done；无检查点=created",
      );
      assert.deepEqual(
        list.map((task) => task.title),
        ["已完成题", "续跑题", "t-created"],
        "标题取题面；缺题面回退任务号（不虚构）",
      );
      assert.deepEqual(
        list.map((task) => task.resumable),
        [false, true, false],
      );
      assert.equal(list[0]?.updatedAt, "2026-10-09T03:00:00Z");
      assert.equal(readLocalHistory(trailDb, checkpointsDb, 2).length, 2, "limit 截断（长历史虚拟列表分页依据）");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("库缺失：留痕库缺失→空列表；检查点库缺失→标题回退且不可续", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-history-missing-"));
    try {
      assert.deepEqual(readLocalHistory(join(dir, "audit.db"), join(dir, "checkpoints.db")), []);
      createTrailDb(join(dir, "audit.db"), [{ taskId: "t1", ts: "2026-10-09T01:00:00Z" }]);
      const list = readLocalHistory(join(dir, "audit.db"), join(dir, "checkpoints.db"));
      assert.equal(list.length, 1);
      assert.equal(list[0]?.title, "t1");
      assert.equal(list[0]?.status, "created");
      assert.equal(list[0]?.resumable, false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("库损坏：留痕库/检查点库各自抛出可读错误（不把未知冒充为无任务）", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-history-broken-"));
    try {
      const trailDb = join(dir, "audit.db");
      const checkpointsDb = join(dir, "checkpoints.db");
      writeFileSync(trailDb, "not-a-sqlite-db");
      assert.throws(() => readLocalHistory(trailDb, checkpointsDb), /本地留痕库读取失败/);

      rmSync(trailDb, { force: true }); // 清除垃圾文件后重建（否则建库语句本身即抛错）
      createTrailDb(trailDb, [{ taskId: "t1", ts: "2026-10-09T01:00:00Z" }]);
      writeFileSync(checkpointsDb, "not-a-sqlite-db");
      assert.throws(() => readLocalHistory(trailDb, checkpointsDb), /本地检查点库读取失败/);
      assert.throws(() => resolveResumeStage(checkpointsDb, "t1"), /本地检查点库读取失败/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("版本偏斜：旧 home 缺 task_inputs 表 → 标题回退任务号，阶段读取不受影响", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-history-legacy-"));
    try {
      const trailDb = join(dir, "audit.db");
      const checkpointsDb = join(dir, "checkpoints.db");
      createTrailDb(trailDb, [{ taskId: "t-legacy", ts: "2026-10-09T01:00:00Z" }]);
      // 旧引擎库：仅有 checkpoints 表（无 task_inputs，题面持久化为后续批次新增）
      const db = new DatabaseSync(checkpointsDb);
      try {
        db.exec(
          "CREATE TABLE checkpoints (task_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL," +
            " step INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL," +
            " PRIMARY KEY (task_id, stage))",
        );
        db.prepare(
          "INSERT INTO checkpoints (task_id, stage, status, step, data, updated_at)" +
            " VALUES (?, ?, ?, 0, '{}', '2026-10-09T00:00:00Z')",
        ).run("t-legacy", "analysis", "done");
      } finally {
        db.close();
      }
      const list = readLocalHistory(trailDb, checkpointsDb);
      assert.equal(list[0]?.title, "t-legacy", "缺题面表 → 标题回退任务号（不虚构）");
      assert.equal(list[0]?.status, "modeling", "阶段读取不受影响");
      assert.equal(list[0]?.resumable, true);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("恢复定位纯函数矩阵：未开始/执行完未过门禁→该阶段；门禁通过推进；全通过→null", () => {
    assert.equal(deriveResumeStage(new Map()), "analysis");
    assert.equal(deriveResumeStage(new Map([["analysis", "done"]])), "modeling");
    assert.equal(
      deriveResumeStage(new Map([["analysis", "stage_done"]])),
      "analysis",
      "执行完未过门禁（stage_done）按「未经门禁不推进」重做该阶段",
    );
    assert.equal(
      deriveResumeStage(
        new Map([
          ["analysis", "done"],
          ["modeling", "done"],
          ["solving", "done"],
          ["writing", "done"],
        ]),
      ),
      null,
    );
  });

  it("resolveResumeStage：部分完成→下一阶段；全完成/无检查点/库缺失→null", () => {
    const dir = mkdtempSync(join(tmpdir(), "erdos-history-resume-"));
    try {
      const checkpointsDb = join(dir, "checkpoints.db");
      assert.equal(resolveResumeStage(checkpointsDb, "t1"), null, "库缺失→null");
      createCheckpointsDb(
        checkpointsDb,
        [{ taskId: "t1", title: "部分题" }],
        [
          { taskId: "t1", stage: "analysis", status: "done" },
          { taskId: "t1", stage: "modeling", status: "done" },
        ],
      );
      assert.equal(resolveResumeStage(checkpointsDb, "t1"), "solving");
      assert.equal(resolveResumeStage(checkpointsDb, "unknown"), null, "无检查点→null");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// 注：桥层（BridgeBackend.handle）不在此直测——其构造链引入 `electron`（safeStorage），
// node:test 无法加载（仓库既有约定：桥层胶水由 typecheck + 壳级冒烟覆盖；本文件的读取器
// 单测与真机 e2e 覆盖桥内全部自研逻辑，桥仅做 3 行参数映射）。

// ---------------------------------------------------------------------------
// 真实引擎续跑集成（FakeLLM）：产出检查点 → 重启 → 客户端定位 → 既有 RPC 续跑
// ---------------------------------------------------------------------------

/** 解析引擎可执行（与 engine-trail.test.ts 同口径）：无则 skip。 */
function resolveEnginePython(): string | null {
  if (process.env.ERDOS_ENGINE_PYTHON) return process.env.ERDOS_ENGINE_PYTHON;
  for (const candidate of [
    join(repoRoot, "engine", ".venv", "Scripts", "python.exe"),
    join(repoRoot, "engine", ".venv", "bin", "python"),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

const enginePython = resolveEnginePython();
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitProgress(events: EngineEvent[], stage: StageName, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (
    !events.some((event) => event.event === "stage.progress" && event.stage === stage && event.progress >= 1) &&
    Date.now() < deadline
  ) {
    await sleep(50);
  }
  return events.some((event) => event.event === "stage.progress" && event.stage === stage && event.progress >= 1);
}
async function waitHostExit(host: EngineHost, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (host.childPid !== null && Date.now() < deadline) await sleep(20);
}

describe("真实引擎续跑集成（FakeLLM）", { skip: enginePython === null }, () => {
  it("重启后：客户端定位 modeling 并经 start_stage 续跑（题面由引擎侧水合，无需补投）", async () => {
    const home = mkdtempSync(join(tmpdir(), "erdos-history-e2e-"));
    const events1: EngineEvent[] = [];
    const host1 = new EngineHost({
      command: enginePython!,
      args: ["-m", "engine"],
      cwd: repoRoot,
      home,
      key: () => null, // FakeLLM 无 Key 模式
      onEvent: (event) => events1.push(event as EngineEvent),
      onStateChange: () => {},
      onLog: () => {},
      onProtocolError: () => {},
    });
    const host2Events: EngineEvent[] = [];
    let host2: EngineHost | null = null;
    try {
      // 1) 运行 analysis 并过门禁（产出检查点：analysis=done）
      await host1.invoke("task_create", { task_id: "hist-1", title: "历史续跑集成", problem_text: "题面：计算 1+1" });
      await host1.invoke("start_stage", { task_id: "hist-1", stage: "analysis" });
      assert.ok(await waitProgress(events1, "analysis", 60_000), "analysis 应在超时前完成");
      await host1.invoke("answer_gate", { task_id: "hist-1", gate: "gate_analysis", decision: "pass" });

      // 2) 模拟 App 重启：停止引擎（优雅退出），客户端侧只读本地库
      host1.stop();
      await waitHostExit(host1);

      const trailDb = join(home, "audit.db");
      const checkpointsDb = join(home, "checkpoints.db");
      const item = readLocalHistory(trailDb, checkpointsDb).find((task) => task.taskId === "hist-1");
      assert.ok(item, "历史列表应包含引擎产出留痕的任务");
      assert.equal(item.title, "历史续跑集成", "标题取自 task_inputs（题面持久化）");
      assert.equal(item.status, "modeling", "状态=下一待完成阶段");
      assert.equal(item.resumable, true);

      const stage = resolveResumeStage(checkpointsDb, "hist-1");
      assert.equal(stage, "modeling", "恢复定位=analysis 之后的 modeling");

      // 题面随 task_create 落盘（引擎重启后水合的依据；「水合进产出」由引擎侧测试覆盖）
      const ckptDb = new DatabaseSync(checkpointsDb, { readOnly: true });
      try {
        const row = ckptDb
          .prepare("SELECT problem_text FROM task_inputs WHERE task_id = ?")
          .get("hist-1") as Record<string, unknown> | undefined;
        assert.ok(String(row?.["problem_text"] ?? "").includes("计算 1+1"), "题面应已落盘");
      } finally {
        ckptDb.close();
      }

      // 3) 重启引擎并经既有 RPC 续跑（等价桥内 engineInvoke；检查点/题面水合由引擎侧承接）
      host2 = new EngineHost({
        command: enginePython!,
        args: ["-m", "engine"],
        cwd: repoRoot,
        home,
        key: () => null,
        onEvent: (event) => host2Events.push(event as EngineEvent),
        onStateChange: () => {},
        onLog: () => {},
        onProtocolError: () => {},
      });
      await host2.invoke("start_stage", { task_id: "hist-1", stage: stage! });
      assert.ok(await waitProgress(host2Events, "modeling", 60_000), "重启后 modeling 应延续完成（无丢任务）");
    } finally {
      if (host1.childPid !== null) host1.stop();
      await waitHostExit(host1);
      if (host2 !== null && host2.childPid !== null) host2.stop();
      if (host2 !== null) await waitHostExit(host2);
      rmSync(home, { recursive: true, force: true });
    }
  });
});