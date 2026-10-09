"""中断-恢复矩阵测试（SP1-2 验收标准）：各阶段崩溃后从最近检查点续跑，无重算。

附：题面持久化（R1）——task_create 落盘 + 重启后 start_stage 水合恢复（方法级 RPC 往返）。
"""

import io
import json
import sqlite3

import pytest

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.rpc import INTERNAL_ERROR
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState
from engine.orchestrator.graph import StageOrchestrator
from engine.orchestrator.pipeline import FakeLLM, StagePipeline
from engine.sandbox.subprocess_sandbox import SubprocessSandbox


async def _run_through(orch: StageOrchestrator, n_stages: int) -> None:
    """跑完前 n_stages 个阶段（每个阶段 run + pass 门禁）。"""
    for _ in range(n_stages):
        await orch.run_current_stage()
        await orch.answer_gate("pass")


@pytest.mark.parametrize(
    "crash_after_stages, expected_resume_stage",
    [
        (0, "analysis"),  # 崩溃于 analysis 开始前
        (1, "modeling"),  # analysis 完成后崩溃
        (2, "solving"),  # modeling 完成后崩溃
        (3, "writing"),  # solving 完成后崩溃
    ],
)
async def test_crash_resume_matrix(tmp_path, crash_after_stages, expected_resume_stage) -> None:
    """崩溃-恢复矩阵：跑完 N 阶段后崩溃，恢复应定位到第 N+1 阶段。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    orch = StageOrchestrator("t1", checkpoint=store)
    await _run_through(orch, crash_after_stages)

    # 模拟崩溃：丢弃 orch，从检查点恢复
    restored = StageOrchestrator.restore("t1", store)
    assert restored.current_stage == expected_resume_stage
    # 前序阶段已恢复，无重算
    assert len(restored.state.stages) == crash_after_stages
    store.close()


async def test_crash_after_gate_reject_retries_same_stage(tmp_path) -> None:
    """门禁驳回后崩溃：恢复后仍在当前阶段重跑。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    orch = StageOrchestrator("t1", checkpoint=store)
    await orch.run_current_stage()  # analysis done
    await orch.answer_gate("reject")  # 驳回，analysis 结果被清除

    # 崩溃后恢复：analysis 结果已清除，仍在 analysis
    restored = StageOrchestrator.restore("t1", store)
    assert restored.current_stage == "analysis"
    assert "analysis" not in restored.state.stages  # 被清除待重跑
    store.close()


async def test_crash_after_final_stage(tmp_path) -> None:
    """最后阶段完成后崩溃：恢复后仍是 writing（最后阶段）。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    orch = StageOrchestrator("t1", checkpoint=store)
    await _run_through(orch, 4)  # 跑完四阶段（writing 也完成并 pass）

    restored = StageOrchestrator.restore("t1", store)
    assert restored.current_stage == "writing"
    assert len(restored.state.stages) == 4  # 四阶段全部恢复
    store.close()


async def test_crash_in_gate_pending_window_rehangs_gate(tmp_path) -> None:
    """门禁未决窗口崩溃：恢复定位到该阶段，重挂门禁且不重放阶段副作用（DEC-005）。

    检查点两态语义（SP1-7）：stage_done=执行完成门禁未决，done=门禁通过；
    未经门禁的阶段恢复后必须重新过门禁，不得静默推进。
    """
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    calls: list[str] = []

    async def runner(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return {"stage": stage, "usage": {"prompt_tokens": 5}}

    orch = StageOrchestrator("t1", checkpoint=store, runner=runner)
    await orch.run_current_stage()  # analysis 执行完成，门禁未决（未 answer_gate）

    restored = StageOrchestrator.restore("t1", store, runner=runner)
    assert restored.current_stage == "analysis"  # 定位到门禁未决阶段（而非跳到 modeling）

    data = await restored.run_current_stage()  # 重挂门禁：阶段不重放
    assert calls == ["analysis"]  # 副作用未重放
    assert data.get("stage") == "analysis"  # 返回检查点数据（usage 经严格脱敏剥离）

    action = await restored.answer_gate("pass")
    assert action["action"] == "next_stage"
    await _run_through(restored, 3)  # modeling/solving/writing 正常续跑
    assert calls == ["analysis", "modeling", "solving", "writing"]

    records = store.completed_stages("t1")
    assert all(r.status == "done" for r in records)  # 门禁通过状态全部升级落库
    store.close()


async def test_restore_forwards_runner_to_remaining_stages(tmp_path) -> None:
    """恢复必须保留 runner：否则剩余阶段静默退化为骨架空产出（SP1-7 恢复演练语义）。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    calls: list[str] = []

    async def runner(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return {"stage": stage}

    orch = StageOrchestrator("t1", checkpoint=store, runner=runner)
    await _run_through(orch, 2)  # analysis + modeling 完成

    restored = StageOrchestrator.restore("t1", store, runner=runner)
    assert restored.current_stage == "solving"
    await _run_through(restored, 2)  # solving + writing 续跑
    assert calls == ["analysis", "modeling", "solving", "writing"]  # 剩余阶段真实执行
    store.close()


async def test_pipeline_resume_hydrates_history_from_checkpoint(tmp_path) -> None:
    """恢复后管线经 state_loader 水合跨阶段上下文（DEC-005）。

    水合失败时 writing 会输出「分析要点：（无）」；水合成功则论文引用前序
    analysis 的真实产出（FakeLLM 确定性文本含「决策变量」）。
    """
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    task_inputs = {"t1": {"title": "恢复演练", "problem_text": "题面文本"}}

    first = StagePipeline(
        sandbox=SubprocessSandbox(timeout=30), work_root=tmp_path / "work",
        task_inputs=task_inputs,
    )
    orch = StageOrchestrator("t1", checkpoint=store, runner=first.process)
    await orch.run_current_stage()  # analysis 完成（落检查点）
    await orch.answer_gate("pass")

    def loader(task_id: str) -> dict:
        return {r.stage: r.data for r in store.completed_stages(task_id)}

    resumed = StagePipeline(
        sandbox=SubprocessSandbox(timeout=30), work_root=tmp_path / "work",
        task_inputs=task_inputs, state_loader=loader,
    )
    restored = StageOrchestrator.restore("t1", store, runner=resumed.process)
    final: dict = {}
    for _ in range(3):  # modeling / solving / writing 续跑
        final = await restored.run_current_stage()
        await restored.answer_gate("pass")

    assert "决策变量" in final["paper_md"]  # 前序分析真实进入论文
    assert "分析要点：（无）" not in final["paper_md"]
    store.close()


# ----------------------------------------------------------------------
# 题面持久化（R1：崩溃/重启后不丢题面）
# ----------------------------------------------------------------------
def _server(task_store=None):
    """最小 RPC 测试装配（与 test_runtime_methods 同口径；仅注入题面存储）。"""
    state = EngineState()
    server = JsonRpcServer(state, EventEmitter(sink=io.StringIO()))
    register_all(server, state, task_store=task_store)
    return server, state


async def _rpc(server: JsonRpcServer, req_id: str, method: str, params: dict) -> dict:
    line = await server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
    )
    return json.loads(line)


async def test_task_input_roundtrip_and_overwrite(tmp_path) -> None:
    """store 级：题面保存/读取/覆盖；未登记返回 None（无题面兼容的判定依据）。"""
    store = SQLiteCheckpointStore(str(tmp_path / "ckpt.db"))
    assert store.load_task_input("t1") is None
    store.save_task_input("t1", "标题", "题面正文")
    assert store.load_task_input("t1") == {"title": "标题", "problem_text": "题面正文"}
    store.save_task_input("t1", "标题2", "题面2")  # 覆盖更新
    assert store.load_task_input("t1") == {"title": "标题2", "problem_text": "题面2"}
    store.close()


async def test_task_create_persists_and_start_stage_hydrates_after_restart(tmp_path) -> None:
    """方法级重启恢复：task_create 落盘 →「重启」（新 state/server，同库）→ start_stage 自动恢复题面。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    server, _ = _server(store)
    resp = await _rpc(
        server, "t0", "task_create",
        {"task_id": "t1", "title": "题面持久化演练", "problem_text": "题面正文：计算 1+1"},
    )
    assert resp["result"]["status"] == "created"
    assert store.load_task_input("t1") == {"title": "题面持久化演练", "problem_text": "题面正文：计算 1+1"}

    # 模拟重启：丢弃内存态，同库重建（state.tasks 为空）
    store2 = SQLiteCheckpointStore(db)
    server2, state2 = _server(store2)
    assert "t1" not in state2.tasks
    resp2 = await _rpc(server2, "s1", "start_stage", {"task_id": "t1", "stage": "analysis"})
    assert "error" not in resp2
    assert state2.tasks["t1"] == {"title": "题面持久化演练", "problem_text": "题面正文：计算 1+1"}
    store.close()
    store2.close()


async def test_start_stage_memory_wins_over_persisted(tmp_path) -> None:
    """内存态优先：同进程已有题面时不被持久化记录覆盖（首次 task_create 的权威性）。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    store.save_task_input("t1", "旧题面", "旧正文")
    server, state = _server(store)
    state.tasks["t1"] = {"title": "内存题面", "problem_text": "内存正文"}
    await _rpc(server, "s1", "start_stage", {"task_id": "t1", "stage": "analysis"})
    assert state.tasks["t1"] == {"title": "内存题面", "problem_text": "内存正文"}
    store.close()


async def test_start_stage_without_task_create_keeps_empty_inputs(tmp_path) -> None:
    """从未登记（骨架/压测路径）→ 空题面兼容语义不变（title=task_id、空题面）。"""
    store = SQLiteCheckpointStore(str(tmp_path / "ckpt.db"))
    server, state = _server(store)
    resp = await _rpc(server, "s1", "start_stage", {"task_id": "t9", "stage": "analysis"})
    assert "error" not in resp
    assert state.tasks["t9"] == {"title": "t9", "problem_text": ""}
    store.close()


async def test_task_create_fail_closed_on_store_error(tmp_path) -> None:
    """落盘失败 fail-loud：可读 RPC 错误 + 内存不留「已登记」（可重试，重启后不会静默丢题面）。"""
    class BrokenStore:
        """桩：题面落盘必失败（模拟磁盘/权限故障）。"""

        def save_task_input(self, *_args) -> None:  # noqa: ANN002
            raise sqlite3.OperationalError("disk I/O error")

        def load_task_input(self, _task_id: str):
            return None

    server, state = _server(task_store=BrokenStore())
    resp = await _rpc(
        server, "t0", "task_create",
        {"task_id": "t1", "title": "标题", "problem_text": "正文"},
    )
    assert resp["error"]["code"] == INTERNAL_ERROR
    assert "题面落盘失败" in resp["error"]["message"]
    assert "t1" not in state.tasks  # 未被「任务已登记」占用（可修复后重试）


async def test_restart_hydrates_task_input_and_reaches_paper(tmp_path) -> None:
    """跨「重启」水合贯通：题面随 task_create 落盘 → 新进程装配（新 state/server，同库）→
    start_stage 自动水合恢复 → 四阶段跑通且论文含原始题面标记（水合题面进入各阶段 prompt/产出）。"""
    marker = "RESTART-INPUT-MARKER-8c1d：重启后题面必须贯通到论文"
    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "checkpoints.db")
    store = SQLiteCheckpointStore(db)

    # 第一阶段进程：登记题面（落盘）
    server, _ = _server(store)
    resp = await _rpc(
        server, "t0", "task_create",
        {"task_id": "r1", "title": "重启贯通", "problem_text": marker},
    )
    assert resp["result"]["status"] == "created"
    store.close()

    # 「重启」：丢弃全部内存态，同库重建（state.tasks 为空）
    store2 = SQLiteCheckpointStore(db)
    state = EngineState()
    events = EventEmitter(sink=io.StringIO())
    server2 = JsonRpcServer(state, events)
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=SubprocessSandbox(timeout=30),
        work_root=home / "tasks",
        task_inputs=state.tasks,
    )
    register_all(server2, state, checkpoint=store2, task_store=store2, runner=pipeline.process, events=events)
    assert "r1" not in state.tasks

    for index, stage in enumerate(["analysis", "modeling", "solving", "writing"]):
        resp = await _rpc(server2, f"s{index}", "start_stage", {"task_id": "r1", "stage": stage})
        assert "error" not in resp, resp
        gate = await _rpc(
            server2, f"g{index}", "answer_gate",
            {"task_id": "r1", "gate": f"gate_{stage}", "decision": "pass"},
        )
        assert gate["result"]["action"] in ("next_stage", "complete")

    paper = (home / "tasks" / "r1" / "paper.md").read_text(encoding="utf-8")
    assert marker in paper  # 水合后的题面进入各阶段 prompt 与论文
    assert "# 重启贯通" in paper
    store2.close()


async def test_task_create_after_restart_rewrites_persisted_input(tmp_path) -> None:
    """重启后（内存为空）重发 task_create：允许且覆盖持久化题面（旧调用方补投路径语义显式化）。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    server, _ = _server(store)
    await _rpc(server, "t0", "task_create", {"task_id": "t1", "title": "旧题面", "problem_text": "旧正文"})

    # 「重启」
    store2 = SQLiteCheckpointStore(db)
    server2, _ = _server(store2)
    resp = await _rpc(server2, "t1", "task_create", {"task_id": "t1", "title": "新题面", "problem_text": "新正文"})
    assert resp["result"]["status"] == "created"
    assert store2.load_task_input("t1") == {"title": "新题面", "problem_text": "新正文"}
    store.close()
    store2.close()
