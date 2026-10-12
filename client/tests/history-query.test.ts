import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve, sep } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { readLocalHistoryPage } from "../main/local-history.ts";
import { parseHistoryQuery, paginateHistory } from "../shared/history.ts";
let root: string; let trail: string; let checkpoints: string;
before(() => {
  root = mkdtempSync(join(tmpdir(), "erdos-history-query-")); trail = join(root, "audit.db"); checkpoints = join(root, "checkpoints.db");
  const audit = new DatabaseSync(trail); const checkpoint = new DatabaseSync(checkpoints);
  try {
    audit.exec("CREATE TABLE audit_trail (task_id TEXT, ts TEXT)");
    checkpoint.exec("CREATE TABLE task_inputs (task_id TEXT, title TEXT); CREATE TABLE checkpoints (task_id TEXT, stage TEXT, status TEXT)");
    const add = audit.prepare("INSERT INTO audit_trail VALUES (?, ?)");
    const title = checkpoint.prepare("INSERT INTO task_inputs VALUES (?, ?)");
    const stage = checkpoint.prepare("INSERT INTO checkpoints VALUES (?, ?, ?)");
    audit.exec("BEGIN"); checkpoint.exec("BEGIN");
    for (let i = 0; i < 120; i++) {
      const id = `task-${String(i).padStart(3, "0")}`;
      add.run(id, new Date(Date.UTC(2026, 9, 12, 0, i)).toISOString()); title.run(id, i === 10 ? "专项旧题 MODEL" : `题目${i}`);
      if (i % 3 === 0) for (const s of ["analysis", "modeling", "solving", "writing"]) stage.run(id, s, "done");
      else if (i % 3 === 2) stage.run(id, "analysis", "done");
    }
    audit.exec("COMMIT"); checkpoint.exec("COMMIT");
  } finally { audit.close(); checkpoint.close(); }
});
after(() => { const rel = relative(resolve(tmpdir()), root); assert.ok(rel && !rel.startsWith(".." + sep)); rmSync(root, { recursive: true, force: true }); });
describe("完整历史查询与分页", () => {
  it("按全部历史查询旧任务，不受默认最近50条限制；三页无重复并保持时间顺序", () => {
    const seen: string[] = [];
    for (const offset of [0, 50, 100]) {
      const page = readLocalHistoryPage(trail, checkpoints, { offset });
      assert.equal(page.total, 120); assert.equal(page.nextOffset, offset === 100 ? null : offset + 50);
      seen.push(...page.items.map(t => t.taskId));
    }
    assert.equal(new Set(seen).size, 120); assert.equal(seen[0], "task-119"); assert.equal(seen.at(-1), "task-000");
    const old = readLocalHistoryPage(trail, checkpoints, { query: "  model " });
    assert.equal(old.total, 1); assert.equal(old.items[0].taskId, "task-010"); assert.equal(old.items[0].title, "专项旧题 MODEL");
    assert.equal(readLocalHistoryPage(trail, checkpoints, { query: "task-010" }).total, 1);
  });
  it("状态/可续筛选与搜索组合；空结果、越过末页、旧库缺失均有准确分页边界", () => {
    for (const status of ["done", "modeling", "created", "resumable"] as const) {
      const page = readLocalHistoryPage(trail, checkpoints, { status, limit: 100 });
      assert.equal(page.total, 40); assert.equal(page.nextOffset, null);
      assert.equal(page.items.every(item => status === "resumable" ? item.resumable : item.status === status), true);
    }
    assert.equal(readLocalHistoryPage(trail, checkpoints, { query: "MODEL", status: "done" }).total, 0);
    assert.deepEqual(readLocalHistoryPage(trail, checkpoints, { offset: 1000000 }), { items: [], total: 120, nextOffset: null });
    assert.deepEqual(readLocalHistoryPage(join(root, "missing"), checkpoints, {}), { items: [], total: 0, nextOffset: null });
    assert.equal(readLocalHistoryPage(trail, join(root, "missing"), { status: "created" }).total, 120);
  });
  it("IPC输入长度/类型/状态/分页边界拒绝；搜索中的SQL字符按字面处理", () => {
    for (const input of [null, [], "query", { query: "x".repeat(129) }, { query: "a\n" }, { query: 1 }, { status: "__proto__" },
      { status: "bad" }, { offset: -1 }, { offset: 1000001 }, { offset: "0" }, { limit: 0 }, { limit: 101 }, { limit: 1.2 }, { path: "any" }]) {
      assert.throws(() => parseHistoryQuery(input), /参数/);
    }
    assert.equal(readLocalHistoryPage(trail, checkpoints, { query: "' OR 1=1 --" }).total, 0);
    assert.deepEqual(paginateHistory([], parseHistoryQuery({})), { items: [], total: 0, nextOffset: null });
  });
});
