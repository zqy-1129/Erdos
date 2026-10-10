import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { TaskController } from "../main/task-controller.ts";
import { InMemorySecretStore, KeyVault, XorEncryptor } from "../main/key-vault.ts";
import type { GetStatusResult, RpcMethod, StageName } from "../shared/ipc.ts";
import type { StageCharge } from "../main/cloud/stage-accounting.ts";
function fixture() {
  const vault = new KeyVault(new XorEncryptor(), new InMemorySecretStore());
  const checkpoints = new Map<string, string>(); let account: string | null = "alice"; let key = true;
  let failReserve = false; let failConfirm = false; let failStart = false; let loseGateResponse = false;
  const charges: string[] = []; const calls: RpcMethod[] = []; let id = 0;
  const status: GetStatusResult = { engine: "idle", task: null };
  const accounting = {
    revalidate: async () => {},
    reserve: async (c: StageCharge): Promise<"online"> => { charges.push("reserve:" + c.execId); if (failReserve) throw new Error("network"); return "online"; },
    confirm: async (c: StageCharge) => { charges.push("confirm:" + c.execId); if (failConfirm) throw new Error("network"); },
    refund: async (c: StageCharge) => { charges.push("refund:" + c.execId); },
  };
  const engine = async (method: RpcMethod, params: Record<string, unknown>): Promise<unknown> => {
    calls.push(method);
    const taskId = String(params.task_id); const stage = params.stage as StageName;
    if (method === "get_status") return structuredClone(status);
    if (method === "start_stage") {
      if (failStart) throw new Error("start refused");
      status.task = { task_id: taskId, stage, status: "running" };
      return { ...status.task };
    }
    if (method === "pause" || method === "resume" || method === "cancel") {
      if (status.task) status.task.status = method === "pause" ? "paused" : method === "resume" ? "running" : "cancelled";
      return {};
    }
    if (method === "answer_gate") {
      const stage = String(params.gate).replace("gate_", "") as StageName;
      const action = params.decision === "reject" ? "retry_stage" : stage === "writing" ? "complete" : "next_stage";
      if (params.decision === "pass") checkpoints.set(taskId + ":" + stage, "done");
      if (loseGateResponse) throw new Error("lost response");
      return { task_id: taskId, gate: params.gate, decision: params.decision, action };
    }
    return {};
  };
  const options = { vault, accounting, engine, account: () => account, hasKey: () => key,
    checkpoint: (taskId: string, stage: StageName) => checkpoints.get(taskId + ":" + stage) ?? null, id: () => "exec-" + ++id };
  return { options, status, charges, calls, checkpoints,
    controller: () => new TaskController(options), account: (v: string | null) => { account = v; }, key: (v: boolean) => { key = v; },
    failReserve: (v: boolean) => { failReserve = v; }, failConfirm: (v: boolean) => { failConfirm = v; },
    failStart: (v: boolean) => { failStart = v; }, loseGateResponse: (v: boolean) => { loseGateResponse = v; } };
}
const start = { task_id: "t-1", stage: "analysis" };
const pass = { task_id: "t-1", gate: "gate_analysis", decision: "pass" };
describe("阶段计费与引擎协调", () => {
  it("四阶段每次预扣、门禁通过后确认，最终重复审批幂等", async () => {
    const f = fixture(); const c = f.controller();
    await c.invoke("task_create", { task_id: "t-1", title: "测试", problem_text: "unit" });
    for (const stage of ["analysis", "modeling", "solving", "writing"]) {
      await c.invoke("start_stage", { ...start, stage });
      f.status.task!.status = "done";
      const result = await c.invoke("answer_gate", { ...pass, gate: "gate_" + stage }) as { action: string };
      assert.equal(result.action, stage === "writing" ? "complete" : "next_stage");
    }
    assert.equal(f.charges.length, 8); assert.equal(c.taskForExec("exec-1"), "t-1");
    assert.deepEqual(f.controller().view("t-1"), { taskId: "t-1", completed: true });
    await c.invoke("answer_gate", { ...pass, gate: "gate_writing" }); assert.equal(f.charges.length, 8);
    await assert.rejects(c.invoke("start_stage", { ...start, stage: "writing" }), /已通过/);
    await assert.rejects(c.invoke("pause", { task_id: "t-1" }), /许可/);
  });
  it("网络丢失重启复用执行号，拒绝重复启动/越阶段和账号串用", async () => {
    const f = fixture(); let c = f.controller(); f.failReserve(true);
    await assert.rejects(c.invoke("start_stage", start));
    f.failReserve(false); c = f.controller(); await c.invoke("start_stage", start);
    assert.deepEqual(f.charges, ["reserve:exec-1", "reserve:exec-1"]);
    await assert.rejects(c.invoke("start_stage", start), /正在执行/);
    await c.invoke("pause", { task_id: "t-1" }); await c.invoke("resume", { task_id: "t-1" });
    await assert.rejects(c.invoke("start_stage", { ...start, stage: "modeling" }));
    f.account("bob"); assert.equal(c.taskForExec("exec-1"), "");
    await assert.rejects(c.invoke("cancel", { task_id: "t-1" }), /其他账号/);
    f.account(null); await assert.rejects(c.invoke("get_status", {}), /登录/);
  });
  it("门禁 reject 复用已预扣许可，pass 后确认失败可重启恢复", async () => {
    const f = fixture(); let c = f.controller();
    await c.invoke("start_stage", start); f.status.task!.status = "done";
    await c.invoke("answer_gate", { ...pass, decision: "reject", feedback: "重写" });
    await c.invoke("start_stage", start); assert.equal(f.charges.filter(v => v.startsWith("reserve")).length, 1);
    f.status.task!.status = "done"; f.failConfirm(true);
    await assert.rejects(c.invoke("answer_gate", pass));
    f.failConfirm(false); c = f.controller(); await c.invoke("answer_gate", pass);
    assert.equal(f.charges.filter(v => v.startsWith("reserve")).length, 1);
    await assert.rejects(c.invoke("start_stage", start), /已通过/);
  });
  it("门禁提交后响应丢失，通过持久检查点确认，不重复推动引擎", async () => {
    const f = fixture(); let c = f.controller();
    await c.invoke("start_stage", start); f.status.task!.status = "done"; f.loseGateResponse(true);
    await assert.rejects(c.invoke("answer_gate", pass));
    c = f.controller(); await c.invoke("get_status", {});
    await c.invoke("answer_gate", pass);
    assert.equal(f.calls.filter(v => v === "answer_gate").length, 1);
    assert.equal(f.charges.filter(v => v.startsWith("confirm")).length, 1);
  });
  it("执行失败和取消退还；受理失败安全退款并允许后续新执行号", async () => {
    const f = fixture(); const c = f.controller();
    f.failStart(true); await assert.rejects(c.invoke("start_stage", start));
    assert.ok(f.charges.includes("refund:exec-1"));
    f.failStart(false); await c.invoke("start_stage", start); f.status.task!.status = "failed";
    await c.invoke("get_status", {}); assert.ok(f.charges.includes("refund:exec-2"));
    await c.invoke("start_stage", start); await c.invoke("cancel", { task_id: "t-1" });
    assert.ok(f.charges.includes("refund:exec-3"));
  });
  it("并发启动串行化、输入和存储损坏拒绝，失败不污染队列", async () => {
    const f = fixture(); const c = f.controller(); f.key(false);
    await assert.rejects(c.invoke("task_create", { task_id: "t-1", title: "x", problem_text: "unit" }), /API Key/);
    await assert.rejects(c.invoke("start_stage", start), /API Key/);
    f.key(true);
    for (const params of [{ ...start, stage: "bogus" }, { ...start, task_id: "../x" }, { ...start, stage: "writing" }]) await assert.rejects(c.invoke("start_stage", params));
    await assert.rejects(c.invoke("answer_gate", { ...pass, gate: "x" }));
    await assert.rejects(c.invoke("answer_gate", pass));
    const attempts = await Promise.allSettled([c.invoke("start_stage", start), c.invoke("start_stage", start)]);
    assert.equal(attempts.filter(a => a.status === "fulfilled").length, 1);
    f.options.vault.save("client.stage-executions.v1", "{}"); assert.throws(() => f.controller(), /账本损坏/);
  });
  it("待启动许可重启可恢复、其他任务不能抢占；引擎未受理时取消仍退还", async () => {
    const f = fixture(); let c = f.controller(); f.failReserve(true);
    await assert.rejects(c.invoke("start_stage", start));
    c = f.controller(); assert.deepEqual(c.pending(), { taskId: "t-1", stage: "analysis", status: "reserving" });
    assert.equal(c.view("t-1").completed, false);
    await assert.rejects(c.invoke("start_stage", { ...start, task_id: "t-2" }), /未结算/);
    await c.invoke("cancel", { task_id: "t-1" });
    assert.equal(c.pending(), null); assert.ok(f.charges.includes("refund:exec-1"));
    assert.equal(f.calls.filter(v => v === "cancel").length, 0);
    f.account("bob"); assert.throws(() => c.view("t-1"), /不可访问/);
    assert.equal(c.pending(), null);
    f.account(null); assert.throws(() => c.view("t-1"), /登录/);
  });
});
