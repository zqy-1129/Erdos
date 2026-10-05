"""6 个 RPC 方法实现（SP1-1 通信骨架 + SP1-2 编排器接入 + EN-WIRE W2 事件回流）。

与 contracts/engine-rpc.schema.json 的 methods 对齐：
start_stage / pause / resume / cancel / get_status / answer_gate

W2 集成：
- register_all 可注入真实 StageRunner（StagePipeline.process）与 EventEmitter；
- start_stage 创建四阶段编排器（可选注入 checkpoint 断点恢复）后以后台任务驱动
  run_current_stage，请求立即返回受理（耗时结果走事件，开发文档 §6.2）；
- 阶段启动/完成发 stage.progress，writing 产物发 artifact.ready；
- cancel 取消在跑的阶段任务（协作取消，沙箱子进程树由沙箱自身超时/强杀兜底）。
"""

import asyncio

from engine.adapters.capabilities import probe_capabilities
from engine.ipc.events import EventEmitter
from engine.ipc.rpc import SCHEMA_UNSUPPORTED, RpcError
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState, TaskState
from engine.orchestrator.graph import StageOrchestrator, StageRunner


class EngineRuntime:
    """在跑阶段任务的运行时句柄：main 退出前 drain，保证事件回流完整。"""

    def __init__(self) -> None:
        # 单任务引擎，按 task_id 记录；W11 将升级为 OperationLog 幂等
        self.running: dict[str, asyncio.Task] = {}

    async def drain(self, cancel: bool = False) -> None:
        """等待（或取消）全部在跑阶段任务。

        cancel=False：stdin EOF 路径——等当前阶段跑完，事件/检查点完整落盘后退出；
        cancel=True：KeyboardInterrupt 路径——协作取消后退出。
        """
        tasks = [t for t in self.running.values() if not t.done()]
        if not tasks:
            return
        if cancel:
            for t in tasks:
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def register_all(
    server: JsonRpcServer,
    state: EngineState,
    checkpoint=None,  # noqa: ANN001 - CheckpointStore | None（避免循环依赖，运行时鸭子类型）
    runner: StageRunner | None = None,
    events: EventEmitter | None = None,
    runtime_info: dict | None = None,  # W14：protocol_version/engine_version/tool_mode/isolation_mode
    key_store=None,  # noqa: ANN001 - KeyStore | None（provider_test 用，Key 不经方法传递）
    probe_transport=None,  # noqa: ANN001 - httpx.AsyncBaseTransport | None（测试注入）
) -> EngineRuntime:
    """注册全部 9 个 RPC 方法；可注入 checkpoint / runner / 事件 / 运行时信息 / 探测依赖。

    返回 EngineRuntime：调用方在事件循环退出前 drain，避免后台阶段任务被静默丢弃。
    """
    runtime = EngineRuntime()
    running = runtime.running
    last_error: dict[str, str] = {}

    def _emit_progress(task_id: str, stage: str, progress: float) -> None:
        if events is None:
            return
        # 事件负载字段为静态构造，schema 校验失败应暴露（fail-loud），不吞异常
        events.emit("stage.progress", task_id=task_id, stage=stage, progress=progress)

    async def _run_stage(task_id: str) -> None:
        """后台驱动当前阶段直至门禁挂起；终态落 task 状态并回流事件。"""
        orch = state.orchestrator
        assert orch is not None  # start_stage 先于本任务创建
        stage = orch.current_stage
        try:
            _emit_progress(task_id, stage, 0.05)
            data = await orch.run_current_stage()
            _emit_progress(task_id, stage, 1.0)
            if events is not None and isinstance(data, dict) and data.get("paper_sha256"):
                events.emit(
                    "artifact.ready", task_id=task_id, artifact="paper", sha256=data["paper_sha256"]
                )
            state.task = TaskState(task_id=task_id, stage=stage, status="done")
        except asyncio.CancelledError:
            state.task = TaskState(task_id=task_id, stage=stage, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - 阶段失败收敛为 failed 状态，错误经 get_status 暴露
            last_error[task_id] = f"{type(exc).__name__}: {exc}"
            state.task = TaskState(task_id=task_id, stage=stage, status="failed")

    async def start_stage(params: dict):
        if not state.protocol_ok:
            # W14：版本协商失败后拒发任务（保留历史，客户端升级后再试）
            raise RpcError(SCHEMA_UNSUPPORTED, "协议版本协商失败（initialize 不匹配），请升级客户端或引擎后重试")
        task_id = params.get("task_id")
        stage = params.get("stage")
        if not task_id or not stage:
            raise ValueError("缺少 task_id 或 stage")
        task = state.start_stage(task_id, stage)
        # SP1-2/W2：创建四阶段编排器（从 stage 开始），注入 checkpoint 与真实 runner
        state.orchestrator = StageOrchestrator(task_id, checkpoint=checkpoint, runner=runner)
        if checkpoint is not None:
            # 若有历史检查点，恢复（断点续跑，不重算已完成阶段）
            restored = StageOrchestrator.restore(task_id, checkpoint)
            if restored.state.stages:
                state.orchestrator = restored
                state.task = TaskState(
                    task_id=task_id, stage=restored.current_stage, status="running"
                )
        # 仅在注入真实 runner 时自动驱动阶段（SP1-1 骨架/压测路径保持纯受理语义）；
        # 耗时结果走事件（stage.progress/artifact.ready），响应立即返回受理。
        if runner is not None:
            running[task_id] = asyncio.create_task(_run_stage(task_id))
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
        task = running.pop(task_id, None)
        if task is not None and not task.done():
            task.cancel()
        state.cancel(task_id)
        return {"task_id": task_id, "status": state.status}

    async def get_status(params: dict):
        snapshot = state.snapshot()
        if last_error:
            snapshot["last_error"] = dict(last_error)
        return snapshot

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

    async def initialize(params: dict):
        """W14 握手：版本协商 + 能力/隔离模式上报；不匹配置 protocol_ok=False 拒发任务。"""
        info = runtime_info or {}
        protocol_version = int(info.get("protocol_version", 1))
        client_version = params.get("client_protocol_version")
        compatible = True
        if client_version is not None and int(client_version) != protocol_version:
            compatible = False
            state.protocol_ok = False
        else:
            state.protocol_ok = True
        return {
            "protocol_version": protocol_version,
            "engine_version": str(info.get("engine_version", "unknown")),
            "compatible": compatible,
            "capabilities": {
                "tool_mode": info.get("tool_mode", "stage_level"),
                "isolation_mode": info.get("isolation_mode", "unknown"),
            },
        }

    async def provider_test(params: dict):
        """W14：端点连通/能力探测（复用 EN-CAP；Key 用引擎已注入的，不经方法传递）。"""
        base_url = params.get("base_url")
        model = params.get("model")
        if not base_url or not model:
            raise ValueError("缺少 base_url 或 model")
        caps = await probe_capabilities(
            str(base_url), str(model),
            provider=str(params.get("provider") or ""),
            keys=key_store, transport=probe_transport, force=True,
        )
        return {
            "ok": caps.models_endpoint,
            "models_endpoint": caps.models_endpoint,
            "tool_mode": caps.tool_mode,
            "probe_source": caps.probe_source,
        }

    async def events_replay(params: dict):
        """W14：seq > after_seq 的缓冲事件按序补发（断线重连后终态事件恢复）。"""
        if events is None:
            return {"events": [], "last_seq": 0}
        after_seq = int(params.get("after_seq", 0))
        limit = int(params.get("limit", 200))
        found = events.replay(after_seq, limit=limit, task_id=params.get("task_id"))
        return {"events": found, "last_seq": events.last_seq}

    server.register("start_stage", start_stage)
    server.register("pause", pause)
    server.register("resume", resume)
    server.register("cancel", cancel)
    server.register("get_status", get_status)
    server.register("answer_gate", answer_gate)
    server.register("initialize", initialize)
    server.register("provider_test", provider_test)
    server.register("events_replay", events_replay)
    return runtime
