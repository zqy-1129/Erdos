"""引擎入口（EN-WIRE W2）：组件装配 + 首行密钥约定 + SIGTERM 优雅退出。

用法（主进程拉起）：
    python -m engine        # 环境变量：ERDOS_ENGINE_HOME（必需）

装配链（真实组件接线，结束"真实链路仅在测试内打通"状态）：
    KeyStore ← stdin 首行 → (OpenAI 适配器 | FakeLLM 无 Key 模式)
      → StagePipeline(sink=留痕 recorder) → StageOrchestrator runner
      → SQLiteCheckpointStore / TrailStore 落 ERDOS_ENGINE_HOME
      → JsonRpcServer(6 方法) + EventEmitter(5 事件)

首行密钥约定（与 client/main/engine-protocol.md §3 对齐）：
- 首行空行或 `ERDOS_NO_KEY` → 无 Key 模式（FakeLLM 离线演示，FakeLLM 仅供测试/演示）；
- 首行为合法 JSON-RPC 请求（含 method 字段）→ 按无 Key 模式运行，该行回放执行（兼容无密钥调用方）；
- 其余首行 → 视为 API Key 一次性注入（读后仅内存持有，不落日志）；
- Key 模式要求环境变量 ERDOS_MODEL_BASE_URL / ERDOS_MODEL_NAME（缺失以退出码 2 拒启，
  禁止猜测默认厂商端点）。

SIGTERM / SIGBREAK（Windows CTRL_BREAK 演练路径）：协作停机 + 300ms 硬上限退出；
检查点均为阶段完成时同步落盘，此处无需额外落盘动作。
"""

import asyncio
import json
import os
import signal
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter
from engine.checkpoint.store import SQLiteCheckpointStore
from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState
from engine.orchestrator.operations import OperationLog
from engine.orchestrator.pipeline import FakeLLM, StagePipeline
from engine.sandbox.subprocess_sandbox import make_sandbox
from engine.tools import build_default_registry
from engine.trail.recorder import TrailRecorder
from engine.trail.store import TrailStore

GRACE_EXIT_SECONDS = 0.3  # SIGTERM 后硬上限（engine-protocol.md §4）


def _engine_version() -> str:
    """引擎版本（与 pyproject/打包版本一致；元数据不可得时回退占位）。"""
    try:
        from importlib.metadata import version

        return version("erdos-engine")
    except Exception:  # noqa: BLE001 - 元数据缺失（未安装场景）回退
        return "0.1.0"


def _fail(message: str) -> NoReturn:
    """诊断性拒启：stderr 输出可读原因，退出码 2（父进程据此转 crashed 语义）。"""
    sys.stderr.write(f"[engine] 拒启：{message}\n")
    sys.stderr.flush()
    raise SystemExit(2)


def _verify_stores(home: Path) -> None:
    """本地库启动自检（EC-D5）：损坏即诊断式拒启，保留现场（不静默重建/清空）。

    F-007 留痕与检查点是恢复与审计的硬依据——带病库上继续写会放大损坏；
    恢复走备份/对账流程（开发文档 §9.2），不在此处自动修复。
    """
    for name in ("audit.db", "checkpoints.db", "operations.db"):
        path = home / name
        if not path.exists():
            continue  # 首次启动：由各存储构造器初始化
        try:
            conn = sqlite3.connect(str(path))
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            _fail(f"本地库损坏：{path.name}（{exc}）；已保留现场，请从备份恢复后重试")
        if row is None or row[0] != "ok":
            _fail(f"本地库损坏：{path.name}（quick_check={row[0] if row else '空'}）；已保留现场，请从备份恢复后重试")


def _read_first_line() -> str:
    """读取 stdin 首行（密钥注入通道）；EOF 视为无 Key。"""
    line = sys.stdin.readline()
    return "" if line == "" else line.rstrip("\r\n")


def _classify_first_line(line: str) -> tuple[bool, str | None]:
    """分类首行 → (是否为 JSON-RPC 请求行, 注入的 Key)。

    Key 不可能同时是含 method 字段的合法 JSON 对象，据此区分三种路径。
    """
    stripped = line.strip()
    if stripped in ("", "ERDOS_NO_KEY"):
        return False, None
    try:
        parsed: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return False, stripped
    if isinstance(parsed, dict) and "method" in parsed:
        return True, None
    return False, stripped


def _build_llm(keys: KeyStore, trail: TrailRecorder):  # noqa: ANN201 - (text_llm, solve_llm_port)
    """按密钥状态装配 LLM：Key 模式走真实 OpenAI 兼容适配器，无 Key 走 FakeLLM。

    返回 (text_llm, solve_llm_port)：text_llm 供四阶段文本生成；solve_llm_port 为
    W11 求解内循环端口（携带 tools），无 Key 模式为 None（tool_loop 不可用）。
    """
    if not keys.has_key:
        return FakeLLM().chat, None
    base_url = os.environ.get("ERDOS_MODEL_BASE_URL")
    model = os.environ.get("ERDOS_MODEL_NAME")
    if not base_url or not model:
        _fail("Key 模式需要环境变量 ERDOS_MODEL_BASE_URL 与 ERDOS_MODEL_NAME")
    config = ModelConfig(
        provider=os.environ.get("ERDOS_MODEL_PROVIDER", "openai-compat"),
        base_url=base_url,
        model=model,
    )
    adapter = OpenAIChatAdapter(config, keys)

    async def llm(messages: list[dict[str, str]], stage: str) -> dict[str, Any]:
        """适配 pipeline 的 _LLM 端口：(messages, stage) → {content, usage, model, stage}。"""
        reply = await adapter.chat(
            [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
        )
        return {
            "content": reply.content,
            "usage": {
                "prompt_tokens": reply.usage.prompt_tokens,
                "completion_tokens": reply.usage.completion_tokens,
            },
            "model": config.model,
            "stage": stage,
        }

    async def solve_llm_port(
        messages: list[ChatMessage], tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> Any:
        """W11 求解内循环端口：携带 tools 的适配器透传（W15：有回调则走流式）。"""
        return await adapter.chat(messages, tools=tools, stream=on_delta is not None, on_delta=on_delta)

    return llm, solve_llm_port


def _build_sink(trail: TrailRecorder):
    """留痕 sink：每次阶段产出后记录 model_call 与 writing 产物（F-007 不可关闭）。"""

    async def sink(task_id: str, stage: str, data: dict[str, Any]) -> None:
        model = data.get("model")
        usage = data.get("usage")
        if model and usage:
            # duration_ms 在此层不可得（计处于 llm 包装内），v1 记 0，W11 收口到统一计量
            trail.record_model_call(task_id, stage, str(model), dict(usage), duration_ms=0.0)
        paper_path = data.get("paper_path")
        if stage == "writing" and paper_path and Path(str(paper_path)).exists():
            trail.record_artifact(task_id, stage, "paper", Path(str(paper_path)))

    return sink


def main() -> None:
    # W17 打包前置：`engine --version` 打印版本并退出（构建时校验引擎版本与 lockfile 绑定，
    # 不依赖 ERDOS_ENGINE_HOME / stdin / 网络，禁运行时动态升级）。
    if "--version" in sys.argv or "-V" in sys.argv:
        print(_engine_version())
        return

    home_raw = os.environ.get("ERDOS_ENGINE_HOME")
    if not home_raw:
        _fail("缺少环境变量 ERDOS_ENGINE_HOME（任务/库数据根目录）")
    home = Path(home_raw)
    home.mkdir(parents=True, exist_ok=True)
    _verify_stores(home)

    first_line = _read_first_line()
    is_request, key = _classify_first_line(first_line)

    keys = KeyStore()
    if key is not None:
        keys.inject(key)

    trail_store = TrailStore(str(home / "audit.db"))
    trail = TrailRecorder(trail_store)
    checkpoint = SQLiteCheckpointStore(str(home / "checkpoints.db"))
    sandbox = asyncio.run(make_sandbox())
    state = EngineState()
    events = EventEmitter()
    registry = build_default_registry(sandbox, events=events, trail=trail)
    operations = OperationLog(str(home / "operations.db"))
    tool_mode = os.environ.get("ERDOS_TOOL_MODE", "stage_level")  # 能力探测就绪前保守默认
    text_llm, solve_llm_port = _build_llm(keys, trail)

    def delta_sink(task_id: str, delta: str) -> None:
        """W15：求解循环 token 增量 → model.delta 事件（节流后；允许丢帧不补发）。"""
        events.emit("model.delta", task_id=task_id, delta=delta[:512])

    def checkpoint_history(task_id: str) -> dict[str, dict[str, Any]]:
        """断点恢复上下文：检查点已完成阶段 → 管线 _history 水合（DEC-005）。"""
        return {record.stage: record.data for record in checkpoint.completed_stages(task_id)}

    pipeline = StagePipeline(
        llm=text_llm,
        sandbox=sandbox,
        sink=_build_sink(trail),
        work_root=home / "tasks",
        registry=registry,
        operations=operations,
        solve_llm=solve_llm_port,
        tool_mode=tool_mode,
        delta_sink=delta_sink,
        task_inputs=state.tasks,
        state_loader=checkpoint_history,
    )

    server = JsonRpcServer(state, events)

    runtime = register_all(
        server, state, checkpoint=checkpoint,
        task_store=checkpoint,  # R1：题面与阶段检查点同库落盘（重启后 start_stage 水合恢复）
        runner=pipeline.process, events=events,
        runtime_info={
            "protocol_version": 2,
            "engine_version": _engine_version(),
            "tool_mode": tool_mode,
            "isolation_mode": getattr(sandbox, "isolation_mode", "unknown"),
        },
        key_store=keys,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _graceful(_signum, _frame):  # noqa: ANN001 - signal 处理签名
        # 协作停机；检查点已在阶段完成时同步落盘，300ms 硬上限兜底强制退出
        server.stop()
        loop.call_later(GRACE_EXIT_SECONDS, lambda: os._exit(0))

    signal.signal(signal.SIGTERM, _graceful)
    if hasattr(signal, "SIGBREAK"):  # Windows：CTRL_BREAK 演练路径
        signal.signal(signal.SIGBREAK, _graceful)

    try:
        loop.run_until_complete(
            server.serve(initial_lines=[first_line] if is_request else None)
        )
        # stdin EOF：等当前阶段收尾（事件/检查点完整落盘）再退出
        loop.run_until_complete(runtime.drain())
    except KeyboardInterrupt:
        server.stop()
        loop.run_until_complete(runtime.drain(cancel=True))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
