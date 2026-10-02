"""6 个 RPC 方法实现（SP1-1 通信骨架 + SP1-2 编排器接入）。

与 contracts/engine-rpc.schema.json 的 methods 对齐：
start_stage / pause / resume / cancel / get_status / answer_gate

SP1-2 集成：start_stage 创建四阶段编排器（可选注入 checkpoint），
answer_gate 驱动门禁（pass 进入下一阶段 / reject 重跑当前阶段）。
"""

from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState, TaskState
from engine.orchestrator.graph import StageOrchestrator


def register_all(server: JsonRpcServer, state: EngineState, checkpoint=None) -> None:
    """注册全部 6 个 RPC 方法；可选注入 checkpoint store（SP1-2 断点续跑）。"""

    async def start_stage(params: dict):
        task_id = params.get("task_id")
        stage = params.get("stage")
        if not task_id or not stage:
            raise ValueError("缺少 task_id 或 stage")
        task = state.start_stage(task_id, stage)
        # SP1-2：创建四阶段编排器（从 stage 开始），注入 checkpoint
        state.orchestrator = StageOrchestrator(task_id, checkpoint=checkpoint)
        if checkpoint is not None:
            # 若有历史检查点，恢复（断点续跑）
            restored = StageOrchestrator.restore(task_id, checkpoint)
            if restored.state.stages:
                state.orchestrator = restored
                state.task = TaskState(
                    task_id=task_id, stage=restored.current_stage, status="running"
                )
        return {"task_id": task.task_id, "stage": task.stage, "status": task.status}

    async def pause(params: dict):
        task_id = params.get("task_id")
        if not task_id:
            raise ValueError("缺少 task_id")
        state.pause(task_id)
        return {"task_id": task_id, "status": state.status}

    async def resume(params: dict):
        task_id = params.get("task_id")
        if not task_id:
            raise ValueError("缺少 task_id")
        state.resume(task_id)
        return {"task_id": task_id, "status": state.status}

    async def cancel(params: dict):
        task_id = params.get("task_id")
        if not task_id:
            raise ValueError("缺少 task_id")
        state.cancel(task_id)
        return {"task_id": task_id, "status": state.status}

    async def get_status(params: dict):
        return state.snapshot()

    async def answer_gate(params: dict):
        task_id = params.get("task_id")
        gate = params.get("gate")
        decision = params.get("decision")
        if not task_id or not gate or decision not in ("pass", "reject"):
            raise ValueError("缺少 task_id/gate 或 decision 非法")
        # SP1-2：驱动编排器门禁
        if state.orchestrator is not None:
            feedback = params.get("feedback", "")
            action = await state.orchestrator.answer_gate(decision, feedback)
            return {"task_id": task_id, "gate": gate, "decision": decision, "action": action["action"]}
        # SP1-1 骨架：仅记录决策
        state.answer_gate(task_id, gate, decision)
        return {"task_id": task_id, "gate": gate, "decision": decision}

    server.register("start_stage", start_stage)
    server.register("pause", pause)
    server.register("resume", resume)
    server.register("cancel", cancel)
    server.register("get_status", get_status)
    server.register("answer_gate", answer_gate)
