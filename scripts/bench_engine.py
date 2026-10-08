"""引擎性能基准（量化验收口径，FakeLLM 通道）。

用法（仓库根）：
    ./.venv/Scripts/python.exe scripts/bench_engine.py [--samples 10] [--out docs/acceptance]

测四项，目标值取自《引擎开发详细方案》§3.2 与 PRD 6.1：
- 引擎冷启动 spawn → initialize 响应 < 2s（p50/p95）；
- 握手往返（同一批样本的响应时间拆分）；
- 沙箱超时强杀精度（elapsed ≤ timeout + 2s）；
- 四阶段墙钟（FakeLLM，验证求解阶段不再空转到执行上限）。

口径纪律（DEC-024）：本脚本只跑 FakeLLM/离线通道，结果不得计入 SP1-7 ≥85% 判定分母；
首 token < 500ms 属真实 Key 通道指标，需 `scripts/run_paper_e2e.py --api-key ...` 单独取样。
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine.ipc.stdio import configure_stdio  # noqa: E402
from engine.sandbox.subprocess_sandbox import SubprocessSandbox  # noqa: E402

COLD_START_TARGET_MS = 2000.0  # spawn → initialize 响应
KILL_SLACK_TARGET_S = 2.0  # 超时强杀相对 timeout 的容差
STAGE_TARGET_MS = {"analysis": 3000.0, "modeling": 3000.0, "solving": 15000.0, "writing": 3000.0}
_CHILD_ENV_EXCLUDED = ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "PYTHONSTARTUP")


def _child_env(home: Path) -> dict[str, str]:
    """引擎子进程环境：剥离覆盖率钩子（否则 GBK 告警会污染协议管道），显式给 HOME。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ERDOS_")
        and k not in _CHILD_ENV_EXCLUDED
        and not k.startswith("COV_CORE_")
    }
    env["ERDOS_ENGINE_HOME"] = str(home)
    return env


def _percentile(values: list[float], pct: float) -> float:
    """最近秩百分位（样本量小，够用即可，不引第三方库）。"""
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def bench_cold_start(samples: int, work_root: Path) -> dict:
    """冷启动 + 握手：逐次拉起引擎、发 initialize、测到响应首字节的墙钟。"""
    req = json.dumps(
        {"jsonrpc": "2.0", "id": "bench-1", "method": "initialize",
         "params": {"client_protocol_version": 2}},
        ensure_ascii=False,
    )
    durations_ms: list[float] = []
    tool_modes: set[str] = set()
    for i in range(samples):
        home = work_root / f"cold-{i}"
        home.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(  # noqa: S603 - 固定解释器与模块，受控参数
            [sys.executable, "-m", "engine"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=str(REPO_ROOT), env=_child_env(home),
        )
        assert proc.stdin is not None and proc.stdout is not None
        started = time.monotonic()
        proc.stdin.write("\n" + req + "\n")  # 首行空行 → 无 Key 模式（FakeLLM）
        proc.stdin.flush()
        line = proc.stdout.readline()
        durations_ms.append(round((time.monotonic() - started) * 1000, 1))
        if line:
            payload = json.loads(line)
            caps = payload.get("result", {}).get("capabilities", {})
            tool_modes.add(str(caps.get("tool_mode", "missing")))
        proc.stdin.close()
        proc.wait(timeout=30)

    return {
        "samples": samples,
        "p50_ms": round(statistics.median(durations_ms), 1),
        "p95_ms": round(_percentile(durations_ms, 95), 1),
        "max_ms": max(durations_ms),
        "target_ms": COLD_START_TARGET_MS,
        "reported_tool_modes": sorted(tool_modes),
        "pass": _percentile(durations_ms, 95) < COLD_START_TARGET_MS,
    }


async def bench_sandbox_kill(work_root: Path, timeout_s: float = 2.0) -> dict:
    """超时强杀精度：死循环代码应在 timeout + 容差内被强杀回收。"""
    sandbox = SubprocessSandbox(timeout=timeout_s)
    started = time.monotonic()
    result = await sandbox.execute(
        code="import time\nwhile True:\n    time.sleep(0.05)",
        files={},
        work_dir=work_root / "kill",
    )
    elapsed = time.monotonic() - started
    return {
        "timeout_s": timeout_s,
        "elapsed_s": round(elapsed, 2),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "error": result.error,
        "stderr_head": result.stderr[:200],
        "target_s": timeout_s + KILL_SLACK_TARGET_S,
        "pass": bool(result.timed_out) and elapsed <= timeout_s + KILL_SLACK_TARGET_S,
    }


async def bench_stage_wall_clock(work_root: Path) -> dict:
    """四阶段墙钟（FakeLLM）：solving 走真实沙箱执行，验证不再空转到执行上限。"""
    from engine.orchestrator.pipeline import FakeLLM, StagePipeline

    sink_calls: list[str] = []

    async def sink(task_id: str, stage: str, data: dict) -> None:
        sink_calls.append(stage)

    sandbox = SubprocessSandbox(timeout=30)
    pipeline = StagePipeline(llm=FakeLLM().chat, sandbox=sandbox, sink=sink,
                             work_root=work_root / "stages")
    measured: dict[str, float] = {}
    for stage in ("analysis", "modeling", "solving", "writing"):
        started = time.monotonic()
        await pipeline.process("bench-task", stage)
        measured[stage] = round((time.monotonic() - started) * 1000, 1)

    breaches = {s: ms for s, ms in measured.items() if ms > STAGE_TARGET_MS[s]}
    return {
        "stage_ms": measured,
        "targets_ms": STAGE_TARGET_MS,
        "sink_stages": sink_calls,
        "pass": not breaches,
        "breaches": breaches,
    }


def _render_md(results: dict) -> str:
    cold = results["cold_start"]
    kill = results["sandbox_kill"]
    stages = results["stage_wall_clock"]
    lines = [
        "# 引擎性能基准（FakeLLM 通道）",
        "",
        f"- 环境：{results['environment']}",
        "- 口径：离线/FakeLLM 通道，按 DEC-024 **不计入** SP1-7 ≥85% 判定分母",
        "",
        "| 指标 | 实测 | 目标 | 判定 |",
        "|---|---|---|---|",
        f"| 冷启动 p50 / p95（spawn→initialize） | {cold['p50_ms']}ms / {cold['p95_ms']}ms"
        f" | < {cold['target_ms']:.0f}ms | {'通过' if cold['pass'] else '未达标'} |",
        f"| 沙箱超时强杀精度 | {kill['elapsed_s']}s（timeout {kill['timeout_s']}s）"
        f" | ≤ {kill['target_s']}s | {'通过' if kill['pass'] else '未达标'} |",
    ]
    for stage, ms in stages["stage_ms"].items():
        target = STAGE_TARGET_MS[stage]
        ok = ms <= target
        lines.append(f"| 阶段墙钟 {stage} | {ms}ms | ≤ {target:.0f}ms | {'通过' if ok else '未达标'} |")
    lines += [
        "",
        f"- initialize 上报 tool_mode：{cold['reported_tool_modes']}",
        f"- 留痕 sink 覆盖阶段：{stages['sink_stages']}",
        "- 未覆盖：首 token < 500ms（EN-STREAM，需真实 Key 通道取样 10 次）、事件吞吐 100 事件/s"
        "（客户端渲染侧压测口径）",
    ]
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> dict:
    work_root = Path(args.work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    results = {
        "environment": f"{sys.version.split()[0]} / {os.name} / {sys.executable}",
        "cold_start": bench_cold_start(args.samples, work_root),
        "sandbox_kill": await bench_sandbox_kill(work_root),
        "stage_wall_clock": await bench_stage_wall_clock(work_root),
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    (out_dir / f"engine-bench-{stamp}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = _render_md(results)
    (out_dir / f"engine-bench-{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    all_pass = all(r["pass"] for r in (results["cold_start"], results["sandbox_kill"],
                                        results["stage_wall_clock"]))
    print(f"[判定] {'PASS' if all_pass else 'FAIL'} · 证据：{out_dir}/engine-bench-{stamp}.{{json,md}}")
    return results


def main() -> None:
    configure_stdio()
    parser = argparse.ArgumentParser(description="引擎性能基准（FakeLLM 通道）")
    parser.add_argument("--samples", type=int, default=10, help="冷启动取样次数")
    parser.add_argument("--out", default="docs/acceptance", help="报告输出目录")
    parser.add_argument("--work-root", default="docs/acceptance/_bench", help="引擎数据根目录")
    args = parser.parse_args()
    results = asyncio.run(_run(args))
    sys.exit(0 if all(r["pass"] for r in (results["cold_start"], results["sandbox_kill"],
                                          results["stage_wall_clock"])) else 1)


if __name__ == "__main__":
    main()
