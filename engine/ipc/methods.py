"""6 个 RPC 方法实现（SP1-1 通信骨架，仅状态机桩，无阶段业务逻辑）。

与 contracts/engine-rpc.schema.json 的 methods 对齐：
start_stage / pause / resume / cancel / get_status / answer_gate
"""

from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState


def register_all(server: JsonRpcServer, state: EngineState) -> None:
    """注册全部 6 个 RPC 方法。"""

    async def start_stage(params: dict):
        task_id = params.get("task_id")
        stage = params.get("stage")
        if not task_id or not stage:
            raise ValueError("缺少 task_id 或 stage")
        task = state.start_stage(task_id, stage)
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
        state.answer_gate(task_id, gate, decision)
        return {"task_id": task_id, "gate": gate, "decision": decision}

    server.register("start_stage", start_stage)
    server.register("pause", pause)
    server.register("resume", resume)
    server.register("cancel", cancel)
    server.register("get_status", get_status)
    server.register("answer_gate", answer_gate)
