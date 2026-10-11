"""报告阶段流式与首 token 判据（W25 · EN-STREAM2，关闭 G14/G18 的可本机验证部分）。

W15 只做了半部：solving 接了 `model.delta`，writing 走的是无流式端口，
判据"首 token ≤500ms"则**从未有任何取样**（G18：`bench_engine.py` 自述未覆盖）。
"能力已实现"与"判据被测量过"是两件事，本文件同时补上：

1. writing 真的产出 `model.delta`，且**终态权威不变**（stage.progress=1.0 / artifact.ready
   仍由阶段结束决定，delta 只是过程展示）；
2. delta 不进回放缓冲（断线重连不补发 token 流，W15 语义）；
3. TTFT 取样器**能被测坏**：把节流参数调到"只在收尾才发射"并拉开块间隔，必须报 FAIL；
   只报一个接近 0 的数的取样器等于没有判据；
4. 10k token 长文本流式聚合无单调内存增长（tracemalloc 峰值对比）；
5. 事件洪水下发射次数与回放缓冲都有界（EC-S6）。

真实厂商通道的达标值仍待凭据（W33/`run_paper_e2e.py --api-key`），按 DEC-024 分开记，
本文件不假装测过。
"""

import asyncio
import importlib.util
import json
import time
import tracemalloc
from pathlib import Path

import httpx

from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter
from engine.ipc.events import EventEmitter
from engine.ipc.throttle import CHAR_THRESHOLD, INTERVAL_SECONDS, DeltaThrottler
from engine.orchestrator.pipeline import FakeLLM, StagePipeline

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bench():
    """按需加载 scripts/bench_engine（scripts 不是包，按文件路径导入）。"""
    spec = importlib.util.spec_from_file_location("bench_engine", REPO_ROOT / "scripts" / "bench_engine.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubSandbox:
    """沙箱端口替身：只满足"solving 阶段要有执行体"，不真跑代码。"""

    isolation_mode = "stub"

    async def execute(self, code, files, work_dir):  # noqa: ANN001
        from engine.sandbox.base import ExecutionResult

        return ExecutionResult(exit_code=0, stdout="stub=1", stderr="", artifacts=[])


def _sink():
    """收事件的计时管道：(event, monotonic 时刻) 序列。"""
    events: list[dict] = []
    stamps: list[float] = []

    class _Sink:
        def write(self, text: str) -> None:
            stamps.append(time.monotonic())
            payload = json.loads(text.strip())
            events.append(payload)

        def flush(self) -> None:
            pass

    return _Sink(), events, stamps


def _adapter(body: bytes) -> OpenAIChatAdapter:
    keys = KeyStore()
    keys.inject("sk-test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return OpenAIChatAdapter(
        ModelConfig("openai-compat", "https://t.test/v1", "m"), keys,
        transport=httpx.MockTransport(handler),
    )


def _sse(chunks: int, size: int) -> bytes:
    nl = chr(10) * 2
    out = []
    for i in range(chunks):
        payload = {"choices": [{"index": 0, "delta": {"content": f"{i:0>{size - 2}}ab"},
                                "finish_reason": None}]}
        out.append("data: " + json.dumps(payload) + nl)
    out.append('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}' + nl)
    out.append('data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}' + nl)
    out.append("data: [DONE]" + nl)
    return "".join(out).encode("utf-8")


# ----------------------------------------------------------------------
# 1/2) writing 阶段流式：有 delta，且终态权威与回放语义不变
# ----------------------------------------------------------------------
def test_writing_stage_emits_model_delta(tmp_path: Path) -> None:
    """报告阶段必须产出 model.delta（W15 缺的那半部）。"""
    sink, events, _ = _sink()
    emitter = EventEmitter(sink=sink)
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=_StubSandbox(),
        work_root=tmp_path,
        delta_sink=lambda task_id, text: emitter.emit("model.delta", task_id=task_id, delta=text[:512]),
    )

    async def run() -> None:
        for stage in ("analysis", "modeling", "solving", "writing"):
            await pipeline.process("t1", stage)

    asyncio.run(run())
    deltas = [e for e in events if e["event"] == "model.delta"]
    assert deltas, "writing 阶段没产出 model.delta，报告期仍是整段闪现"
    assert all(e["task_id"] == "t1" for e in deltas)


def test_writing_delta_keeps_terminal_authority(tmp_path: Path) -> None:
    """delta 只是过程展示：论文与终态产物才是权威（delta 不得进回放缓冲）。"""
    sink, events, _ = _sink()
    emitter = EventEmitter(sink=sink)
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=_StubSandbox(),
        work_root=tmp_path,
        delta_sink=lambda task_id, text: emitter.emit("model.delta", task_id=task_id, delta=text[:512]),
    )

    async def run() -> None:
        for stage in ("analysis", "modeling", "solving", "writing"):
            await pipeline.process("t1", stage)

    asyncio.run(run())
    replayed = emitter.replay(after_seq=0)
    assert not [e for e in replayed if e["event"] == "model.delta"], (
        "token 流进了回放缓冲：断线重连会补发一堆历史增量，渲染层会重复输出"
    )
    paper = tmp_path / "t1" / "paper.md"  # work_root 直下即任务目录（装配层才额外套 tasks/）
    assert paper.exists(), "接了流式反而弄坏终态产物"


def test_no_delta_sink_keeps_port_non_streaming(tmp_path: Path) -> None:
    """正向对照：没给 delta_sink 时 writing 不传 on_delta（默认仍是非流式）。"""
    seen: dict = {}

    async def spy_llm(messages, stage, on_delta=None):  # noqa: ANN001
        seen[stage] = on_delta
        return {
            "content": "【摘要】a【结论】b" if stage == "writing" else "x",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "model": "spy",
        }

    pipeline = StagePipeline(llm=spy_llm, work_root=tmp_path)

    async def run() -> None:
        await pipeline.process("t1", "writing")

    asyncio.run(run())
    assert seen["writing"] is None


# ----------------------------------------------------------------------
# 3) TTFT 取样器能被测坏
# ----------------------------------------------------------------------
def test_ttft_probe_reports_pass_on_fast_local_stream() -> None:
    bench = _bench()
    result = asyncio.run(bench.ttft_probe(3))
    assert result["channel"] == "fake-local-transport"
    assert result["pass"] is True
    assert 0 <= result["p95_ms"] <= bench.TTFT_TARGET_MS
    assert "非真实厂商测量" in result["note"], "取样口径必须自带来源标注（DEC-024）"


def test_ttft_probe_can_detect_a_violation() -> None:
    """判据的反证：让节流器"只在收尾才发射"并拉开块间隔 → 必须报 FAIL。

    没有这条，取样器就是一台恒绿的表——本项目栽过好几次"指标永远为 0 看着像健康"。
    """
    bench = _bench()
    slow = asyncio.run(
        bench.ttft_probe(
            1,
            interval=60.0,          # 时间阈值远大于整段流时长 → 永不因时间触发发射
            char_threshold=10**9,   # 字符阈值同理 → 只剩收尾 flush 会发射
            gap_s=0.03,             # 模拟 token 间隔（20 块 → 约 0.6s > 500ms 判据）
            chunks=20,
        )
    )
    assert slow["pass"] is False, slow
    assert slow["p95_ms"] > bench.TTFT_TARGET_MS, slow


def test_ttft_probe_defaults_match_throttler_constants() -> None:
    """取样器的默认节流参数必须等于生产常量——否则测的是另一套配置。"""
    bench = _bench()
    result = asyncio.run(bench.ttft_probe(1))
    assert result["throttle_interval_ms"] == round(INTERVAL_SECONDS * 1000, 1)
    assert result["throttle_char_threshold"] == CHAR_THRESHOLD


# ----------------------------------------------------------------------
# 4) 10k token 长文本：流式聚合不得单调吃内存
# ----------------------------------------------------------------------
def test_long_stream_aggregation_does_not_grow_monotonically() -> None:
    """10k token 量级（≈40k 字符）重复聚合，tracemalloc 峰值不得逐次上台阶。"""
    body = _sse(2000, 20)  # 2000 块 × 20 字符 ≈ 40k 字符
    adapter = _adapter(body)
    peaks: list[int] = []

    async def one() -> None:
        collected: list[str] = []
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            reply = await adapter.chat(
                [ChatMessage(role="user", content="长文")],
                stream=True,
                on_delta=lambda d: collected.append(d),
            )
            peaks.append(max(0, tracemalloc.get_traced_memory()[1] - before))
            assert reply.content and len("".join(collected)) == len(reply.content)
        finally:
            tracemalloc.stop()

    for _ in range(3):
        asyncio.run(one())
    assert len(peaks) == 3
    drift = peaks[-1] - peaks[0]
    assert drift < max(1_000_000, peaks[0] // 2), (
        f"三次聚合峰值单调上台阶（{peaks}）：流式路径可能有未释放的累积引用"
    )


def test_stream_aggregation_keeps_vendor_usage() -> None:
    """同一条流式路径的用量必须仍是厂商给的（W8 请求侧已带 include_usage）。"""
    adapter = _adapter(_sse(5, 20))

    async def run() -> None:
        reply = await adapter.chat([ChatMessage(role="user", content="hi")], stream=True,
                                   on_delta=lambda _d: None)
        assert reply.usage.estimated is False
        assert reply.usage.prompt_tokens == 11

    asyncio.run(run())


# ----------------------------------------------------------------------
# 5) 事件洪水：发射次数与缓冲都有界（EC-S6）
# ----------------------------------------------------------------------
def test_event_flood_stays_bounded() -> None:
    emitted: list[str] = []
        # interval 给大：只让字符阈值起作用，才能验证"合并发射"真的在合并
    throttler = DeltaThrottler(emit=emitted.append, interval=3600.0, char_threshold=CHAR_THRESHOLD)
    for i in range(5000):
        throttler.push(f"{i:0>5}")  # 每次 5 字符
    throttler.flush()

    total_chars = sum(len(e) for e in emitted)
    assert total_chars == 5000 * 5, f"合并发射不得丢内容：{total_chars}"
    assert len(emitted) <= (5000 * 5) // CHAR_THRESHOLD + 2, (
        f"发射次数没被字符阈值压住：{len(emitted)} 次"
    )

    sink, events, _ = _sink()
    emitter = EventEmitter(sink=sink, buffer_size=100)
    for _ in range(500):
        emitter.emit("stage.progress", task_id="t1", stage="writing", progress=0.5)
    assert len(events) == 500, "事件必须都发出去（洪水不能靠丢事件解决）"
    assert len(emitter.replay(after_seq=0)) == 100, "回放缓冲必须有界（环形），否则洪水会撑爆内存"
