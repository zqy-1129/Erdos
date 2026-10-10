import { randomUUID } from "node:crypto";
import { STAGE_POINTS, parseTaskInput } from "../shared/product.ts";
import type { AnswerGateResult, GetStatusResult, RpcMethod, StageName } from "../shared/ipc.ts";
import type { StageAccountingPort, StageCharge } from "./cloud/stage-accounting.ts";
import type { KeyVault } from "./key-vault.ts";

type ChargeStatus = "reserving" | "reserved" | "gate_pending" | "confirm_pending" | "confirmed" | "refund_pending" | "refunded";
interface Execution extends StageCharge { account: string; status: ChargeStatus; gateResult?: AnswerGateResult }
export interface TaskControllerOptions {
  engine: (method: RpcMethod, params: Record<string, unknown>) => Promise<unknown>;
  accounting: StageAccountingPort;
  vault: KeyVault;
  account: () => string | null;
  hasKey: () => boolean;
  checkpoint: (taskId: string, stage: StageName) => string | null;
  id?: () => string;
}
const SCOPE = "client.stage-executions.v1";
const STAGES: StageName[] = ["analysis", "modeling", "solving", "writing"];
const STATUSES = ["reserving", "reserved", "gate_pending", "confirm_pending", "confirmed", "refund_pending", "refunded"];
const terminal = (r: Execution): boolean => ["confirmed", "refunded"].includes(r.status);

/** 串行协调引擎与账本。先持久化意图，再调用外部副作用；检查点证明门禁已提交。
 * O(n)，保留最近 500 条终态记录；未结算流水与任务归属不会被裁剪。
 */
export class TaskController {
  private readonly options: TaskControllerOptions;
  private records: Execution[];
  private owners: Record<string, string>;
  private tail: Promise<unknown> = Promise.resolve();
  constructor(options: TaskControllerOptions) {
    this.options = options;
    try {
      this.records = JSON.parse(options.vault.load(SCOPE) ?? "[]") as Execution[];
      const owners: unknown = JSON.parse(options.vault.load(SCOPE + ".owners") ?? "{}");
      if (!owners || typeof owners !== "object" || Array.isArray(owners) ||
          !Object.values(owners).every(v => typeof v === "string")) throw new Error();
      this.owners = Object.assign(Object.create(null) as Record<string, string>, owners);
      if (!Array.isArray(this.records) || !this.records.every(r => r && typeof r.execId === "string" &&
          typeof r.account === "string" && typeof r.taskId === "string" && STAGES.includes(r.stage) &&
          r.points === STAGE_POINTS[r.stage] && ["online", "offline"].includes(r.mode) && STATUSES.includes(r.status))) throw new Error();
      if (new Set(this.records.map(r => r.execId)).size !== this.records.length) throw new Error();
    } catch { throw new Error("本地阶段账本损坏，需核对后恢复，不能重新扣费"); }
  }
  invoke(method: RpcMethod, params: Record<string, unknown>): Promise<unknown> {
    const work = this.tail.then(() => this.dispatch(method, params));
    this.tail = work.catch(() => undefined);
    return work;
  }
  taskForExec(execId: string): string {
    return this.records.find(r => r.execId === execId && r.account === this.options.account())?.taskId ?? "";
  }
  pending(): { taskId: string; stage: StageName; status: string } | null {
    const record = [...this.records].reverse().find(r => r.account === this.options.account() && !terminal(r));
    return record ? { taskId: record.taskId, stage: record.stage, status: record.status } : null;
  }
  /** 任务完成须同时具备写作门禁检查点与已结算账本，避免把待确认消费显示为完成。 */
  view(taskId: string): { taskId: string; completed: boolean } {
    const account = this.options.account();
    if (!account) throw new Error("请先登录后操作任务");
    if (!/^[\w-]{1,36}$/.test(taskId) || this.owners[taskId] !== account) throw new Error("任务不可访问");
    return { taskId, completed: this.options.checkpoint(taskId, "writing") === "done" && !this.current(taskId) };
  }
  private commit(record: Execution): void {
    const next = this.records.filter(r => r.execId !== record.execId);
    next.push(record);
    const kept = [...next.filter(terminal).slice(-500), ...next.filter(r => !terminal(r))];
    this.options.vault.save(SCOPE, JSON.stringify(kept));
    this.records = kept;
  }
  private own(taskId: string, account: string): void {
    if (!/^[\w-]{1,36}$/.test(taskId)) throw new Error("任务号无效");
    const owner = this.owners[taskId] ?? this.records.find(r => r.taskId === taskId)?.account;
    if (owner && owner !== account) throw new Error("此任务属于其他账号");
    if (!owner) {
      const next = Object.assign(Object.create(null) as Record<string, string>, this.owners, { [taskId]: account });
      this.options.vault.save(SCOPE + ".owners", JSON.stringify(next));
      this.owners = next;
    }
  }
  private current(taskId: string, stage?: StageName): Execution | undefined {
    return [...this.records].reverse().find(r => r.account === this.options.account() && r.taskId === taskId &&
      (!stage || r.stage === stage) && !terminal(r));
  }
  private result(record: Execution): AnswerGateResult {
    return record.gateResult ?? { task_id: record.taskId, gate: "gate_" + record.stage, decision: "pass",
      action: record.stage === "writing" ? "complete" : "next_stage" };
  }
  private async recover(record: Execution): Promise<void> {
    if (["reserved", "gate_pending"].includes(record.status) &&
        this.options.checkpoint(record.taskId, record.stage) === "done") {
      record = { ...record, status: "confirm_pending", gateResult: this.result(record) };
      this.commit(record);
    }
    if (record.status === "confirm_pending") {
      await this.options.accounting.confirm(record);
      this.commit({ ...record, status: "confirmed" });
    } else if (record.status === "refund_pending") {
      await this.options.accounting.refund(record);
      this.commit({ ...record, status: "refunded" });
    }
  }
  private async refund(record: Execution): Promise<void> {
    if (this.options.checkpoint(record.taskId, record.stage) === "done") {
      await this.recover(record);
      return;
    }
    const pending: Execution = { ...record, status: "refund_pending" };
    this.commit(pending);
    await this.recover(pending);
  }
  private async dispatch(method: RpcMethod, params: Record<string, unknown>): Promise<unknown> {
    const account = this.options.account();
    if (!account) throw new Error("请先登录后操作任务");
    if (method === "task_create") {
      const input = parseTaskInput(params);
      if (!this.options.hasKey()) throw new Error("请先配置自己的 API Key 和模型");
      this.own(input.task_id, account);
      return this.options.engine(method, { ...input });
    }
    const taskId = typeof params.task_id === "string" ? params.task_id : "";
    if (["start_stage", "answer_gate", "cancel", "pause", "resume"].includes(method)) this.own(taskId, account);
    if (method === "start_stage") {
      const stage = params.stage as StageName;
      if (!STAGES.includes(stage)) throw new Error("阶段无效");
      if (!this.options.hasKey()) throw new Error("请先配置自己的 API Key 和模型");
      for (const previous of this.records.filter(r => r.account === account && !terminal(r) && (r.taskId !== taskId || r.stage !== stage))) {
        await this.recover(previous);
        if (this.current(previous.taskId, previous.stage)) throw new Error("已有未结算阶段，请恢复或取消该任务后继续");
      }
      let record = this.current(taskId, stage);
      if (record) await this.recover(record);
      if (this.options.checkpoint(taskId, stage) === "done" ||
          this.records.some(r => r.account === account && r.taskId === taskId && r.stage === stage && r.status === "confirmed")) {
        throw new Error("该阶段已通过门禁；请继续下一阶段或新建任务");
      }
      record = this.current(taskId, stage);
      if (record?.status === "gate_pending") throw new Error("门禁提交结果待恢复，请重试门禁审批");
      const status = await this.options.engine("get_status", {}) as GetStatusResult;
      if (status.task && ["running", "paused"].includes(status.task.status)) throw new Error("已有阶段正在执行，请先继续或取消");
      if (status.orchestrator?.task_id === taskId && status.orchestrator.current_stage !== stage) throw new Error("请按阶段顺序继续任务");
      if (STAGES.slice(0, STAGES.indexOf(stage)).some(s => this.options.checkpoint(taskId, s) !== "done")) throw new Error("前序阶段尚未通过门禁");
      record ??= { execId: (this.options.id ?? randomUUID)(), taskId, stage, points: STAGE_POINTS[stage],
        account, mode: "online", status: "reserving" };
      this.commit(record);
      const reused = record.status === "reserved";
      if (record.status === "reserving") {
        const mode = await this.options.accounting.reserve(record);
        record = { ...record, mode, status: "reserved" };
        this.commit(record);
      }
      if (reused) await this.options.accounting.revalidate(record);
      try { return await this.options.engine(method, params); }
      catch (error) {
        // 响应不明先查询；引擎仍运行或查询失败时保留许可，不提前退款。
        try {
          const observed = await this.options.engine("get_status", {}) as GetStatusResult;
          if (observed.task?.task_id !== taskId || ["failed", "cancelled"].includes(observed.task.status)) await this.refund(record);
        } catch { /* 下次 get_status / cancel 按同一执行号恢复。 */ }
        throw error;
      }
    }
    if (method === "answer_gate") {
      const stage = String(params.gate ?? "").replace(/^gate_/, "") as StageName;
      if (!STAGES.includes(stage) || !["pass", "reject"].includes(String(params.decision))) throw new Error("门禁或决策无效");
      const completed = this.records.find(r => r.account === account && r.taskId === taskId && r.stage === stage && r.status === "confirmed");
      if (completed && params.decision === "pass") return this.result(completed);
      let record = this.current(taskId, stage);
      if (!record || !["reserved", "gate_pending", "confirm_pending"].includes(record.status)) throw new Error("没有可审批的阶段许可，请从检查点恢复任务");
      await this.recover(record);
      if (!this.current(taskId, stage)) return this.result(record);
      if (record.status === "gate_pending" && params.decision !== "pass") throw new Error("门禁通过结果待核对，请先重试通过操作");
      if (params.decision === "pass") {
        record = { ...record, status: "gate_pending" };
        this.commit(record);
      }
      const result = await this.options.engine(method, params) as AnswerGateResult;
      if (params.decision === "pass") {
        if (!["next_stage", "complete"].includes(result.action ?? "")) throw new Error("门禁响应无效，需核对检查点后重试");
        const pending: Execution = { ...record, status: "confirm_pending", gateResult: result };
        this.commit(pending);
        await this.recover(pending);
      }
      return result;
    }
    if (method === "pause" || method === "resume") {
      const record = this.current(taskId);
      if (!record || record.status !== "reserved") throw new Error("当前任务没有有效阶段许可");
      if (method === "resume") await this.options.accounting.revalidate(record);
      return this.options.engine(method, params);
    }
    if (method === "cancel") {
      const observed = await this.options.engine("get_status", {}) as GetStatusResult;
      const result = observed.task?.task_id === taskId ? await this.options.engine(method, params) : { task_id: taskId, status: "idle" };
      const record = this.current(taskId);
      if (record) await this.refund(record);
      return result;
    }
    const result = await this.options.engine(method, params);
    if (method === "get_status") {
      const status = result as GetStatusResult;
      for (const pending of [...this.records].filter(r => r.account === account && !terminal(r))) await this.recover(pending);
      const record = status.task ? this.current(status.task.task_id, status.task.stage) : undefined;
      if (record && ["failed", "cancelled"].includes(status.task?.status ?? "")) await this.refund(record);
    }
    return result;
  }
}
