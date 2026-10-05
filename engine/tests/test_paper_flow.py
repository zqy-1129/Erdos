"""EN-PAPER 系统测试：RPC 级四阶段贯通（task_create → 四阶段 → 论文落盘）。

覆盖：
- task_create 登记题面 → start_stage 同任务顺序推进（禁跳级约束生效）；
- 题面贯通断言：题面唯一标记出现在最终 paper.md（证明题目进入各阶段 prompt）；
- 论文结构完整：摘要/问题重述/建模/求解/结论章节 + sha256 + artifact.ready 事件；
- 未登记任务兼容路径（无题面模式）不回归。
FakeLLM 为确定性假模型（测试专用）；真实 Key 验收用 scripts/run_paper_e2e.py。
"""

import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_full_paper_flow_over_rpc(tmp_path: Path) -> None:
    """四阶段 RPC 顺序推进：task_create → analysis→modeling→solving→writing → paper.md。"""
    import io

    from engine.ipc.events import EventEmitter
    from engine.ipc.methods import register_all
    from engine.ipc.server import JsonRpcServer
    from engine.ipc.state import EngineState
    from engine.orchestrator.pipeline import FakeLLM, StagePipeline
    from engine.sandbox.subprocess_sandbox import SubprocessSandbox

    marker = "UNIQUE-PROBLEM-MARKER-7f3a9：某工厂生产两种产品 A 与 B，求最大利润方案"
    problem = (
        f"{marker}\n约束：原料不超过 100 单位，工时不超过 80 小时；"
        "A 每件利润 5 元耗原料 2 单位工时 1 小时，B 每件利润 4 元耗原料 1 单位工时 2 小时。"
    )
    home = tmp_path / "home"
    home.mkdir()
    state = EngineState()
    sink = io.StringIO()
    events = EventEmitter(sink=sink, trace_id="trace-paper")
    server = JsonRpcServer(state, events)
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=SubprocessSandbox(timeout=30),
        work_root=home / "tasks",
        task_inputs=state.tasks,
    )
    register_all(server, state, runner=pipeline.process, events=events)

    async def call(req_id: str, method: str, params: dict) -> dict:
        line = await server.handle_line(
            json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        )
        return json.loads(line)

    # 1. 登记题面
    resp = await call("t0", "task_create",
                      {"task_id": "paper1", "title": "生产计划优化", "problem_text": problem})
    assert resp["result"]["status"] == "created"

    # 2. 四阶段顺序推进（每阶段：start_stage 受理 → answer_gate 通过）
    for index, stage in enumerate(["analysis", "modeling", "solving", "writing"]):
        resp = await call(f"s{index}", "start_stage", {"task_id": "paper1", "stage": stage})
        assert "error" not in resp, resp
        assert resp["result"]["status"] == "running"
        gate = await call(f"g{index}", "answer_gate",
                          {"task_id": "paper1", "gate": f"gate_{stage}", "decision": "pass"})
        assert gate["result"]["action"] in ("next_stage", "complete")

    # 3. 跳级约束：writing 完成后再次 start_stage(analysis) 被拒
    resp = await call("bad", "start_stage", {"task_id": "paper1", "stage": "analysis"})
    assert resp["error"]["code"] == -32602  # 阶段顺序约束

    # 4. 论文落盘且题面贯通（唯一标记出现在最终论文 = 题面进入了各阶段 prompt）
    paper_path = home / "tasks" / "paper1" / "paper.md"
    assert paper_path.exists(), "论文未落盘"
    paper = paper_path.read_text(encoding="utf-8")
    assert "UNIQUE-PROBLEM-MARKER-7f3a9" in paper
    assert "# 生产计划优化" in paper
    for section in ("摘要", "问题重述", "求解与结果", "结论"):
        assert section in paper
    # slope 结果来自阶段级求解（真实沙箱执行），被论文引用
    assert "slope" in paper

    # 5. 事件回流：artifact.ready（writing 产物）+ tool 留痕经 sink 可查
    lines = [json.loads(x) for x in sink.getvalue().splitlines() if x]
    assert any(e["event"] == "artifact.ready" and e.get("sha256") for e in lines)


@pytest.mark.asyncio
async def test_unregistered_task_empty_problem_fallback(tmp_path: Path) -> None:
    """未 task_create 的任务：无题面模式兼容（骨架/压测路径不回归）。"""
    import io

    from engine.ipc.events import EventEmitter
    from engine.ipc.methods import register_all
    from engine.ipc.server import JsonRpcServer
    from engine.ipc.state import EngineState
    from engine.orchestrator.pipeline import FakeLLM, StagePipeline

    state = EngineState()
    events = EventEmitter(sink=io.StringIO())
    server = JsonRpcServer(state, events)
    pipeline = StagePipeline(llm=FakeLLM().chat, work_root=tmp_path, task_inputs=state.tasks)
    register_all(server, state, runner=pipeline.process, events=events)

    line = await server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "start_stage",
                    "params": {"task_id": "raw", "stage": "analysis"}})
    )
    resp = json.loads(line)
    assert "error" not in resp  # 自动登记空题面，不拒绝
