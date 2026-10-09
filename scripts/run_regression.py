"""SP1-7 回归一键驱动（验收方案步骤 2 基线护栏 / 步骤 3 真实 Key 预置）。

用法（仓库根目录）：
    python scripts/run_regression.py                      # 进程内 FakeLLM 全量 20 题（基线护栏）
    python scripts/run_regression.py --per-category 1     # 每类抽 1 题快速回归
    python scripts/run_regression.py --sandbox docker     # Docker 沙箱模式（不可用即拒启）
    python scripts/run_regression.py --no-kill-resume     # 关闭断点恢复演练
    python scripts/run_regression.py --driver rpc         # 驱动真实引擎进程（无 Key=FakeLLM 系统级护栏）
    python scripts/run_regression.py --driver rpc \
        --api-key sk-xxx --base-url https://api.deepseek.com/v1 \
        --model deepseek-chat --provider deepseek         # 真实 Key 通道（判定通道，11-05~11-14 窗口）
    python scripts/run_regression.py --driver rpc --api-key ... --gate rubric \
        --base-url ... --model ...                        # 同上，但门禁按 rubric 真实评审

通道语义（DEC-024）：--driver inproc（默认）与无 Key rpc 均为 FakeLLM 护栏通道，
不计入 ≥85% 判定分母；真实 Key 通道结果以 CHANNEL_REAL 登记，decision() 出 Go/No-Go。
门禁语义：--gate autopass（默认）沿用基线的自动通过；--gate rubric 走 SP1-3 LLM 评委
（SP1-7「门禁通过为硬条件」的机器判定），评委与被测模型同一 BYOK 通道，消耗如实计入
summary.json 的 judge_usage；rubric 门禁要求真实 Key 通道——FakeLLM 不是评委，用它评审
只会得到恒定不通过（污染判定分母），故直接拒启。
产物：docs/acceptance/sp1-7/{business_id}.json + summary.json + report.md。
护栏语义：FakeLLM 通道成功率必须 100%，否则退出码 1（确定性链路破防）。
"""

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter
from engine.gates.llm_evaluator import LlmRubricEvaluator
from engine.gates.schema import load_rubric_for_stage
from engine.ipc.stdio import configure_stdio
from engine.orchestrator.graph import STAGES
from engine.regression.evidence import EvidenceWriter
from engine.regression.flow import FakeLLMFlow
from engine.regression.problems import REGRESSION_SET, stratified_sample
from engine.regression.runner import (
    CHANNEL_BASELINE,
    CHANNEL_REAL,
    AcceptanceRunner,
    RubricGatePolicy,
)

RUBRICS_DIR = REPO_ROOT / "engine" / "gates" / "rubrics"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SP1-7 真题回归一键驱动")
    parser.add_argument("--per-category", type=int, default=None,
                        help="每类题型抽取题数（默认全量 20 题）")
    parser.add_argument("--evidence", default=str(REPO_ROOT / "docs" / "acceptance" / "sp1-7"),
                        help="证据落盘目录（默认 docs/acceptance/sp1-7/）")
    parser.add_argument("--sandbox", choices=("subprocess", "docker"), default="subprocess",
                        help="沙箱模式（验收矩阵要求双模式各 ≥5 题）")
    parser.add_argument("--no-kill-resume", action="store_true",
                        help="关闭断点恢复演练（默认每类题型抽 1 题在 modeling 后 kill）")
    parser.add_argument("--work-root", default=None,
                        help="工作目录根（默认系统临时目录）")
    parser.add_argument("--driver", choices=("inproc", "rpc"), default="inproc",
                        help="inproc=进程内 FakeLLM（基线）；rpc=真实引擎进程 JSON-RPC")
    parser.add_argument("--api-key", default=None, help="厂商 API Key（仅经 stdin 首行注入引擎）")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容 Base URL（Key 模式必需）")
    parser.add_argument("--model", default=None, help="模型名（Key 模式必需）")
    parser.add_argument("--provider", default=None, help="厂商族（能力矩阵键，如 deepseek）")
    parser.add_argument("--stage-timeout", type=float, default=900.0,
                        help="rpc 驱动单阶段超时秒数（默认 900）")
    parser.add_argument("--gate", choices=("autopass", "rubric"), default="autopass",
                        help="门禁口径：autopass=基线自动通过；rubric=SP1-3 LLM 评委真实评审"
                             "（需真实 Key 通道，SP1-7 判定用）")
    return parser.parse_args()


def _build_factory(args: argparse.Namespace):
    """任务流工厂（依赖注入：驱动方式 / 沙箱模式 / 工作目录）。"""
    import tempfile

    work_root = Path(args.work_root) if args.work_root else Path(tempfile.mkdtemp(prefix="erdos-reg-"))
    print(f"[工作目录] {work_root}")

    if args.driver == "rpc":
        from engine.regression.rpc_flow import RpcTaskFlow

        extra_env: dict[str, str] = {}
        if args.sandbox == "docker":
            from engine.sandbox.subprocess_sandbox import detect_docker_available

            if not asyncio.run(detect_docker_available()):
                sys.exit("要求 Docker 沙箱但环境不可用；请安装并启动 Docker Desktop 后重试")
            extra_env["ERDOS_SANDBOX_REQUIRE_DOCKER"] = "1"  # 引擎侧强制 Docker（DEC-006）

        def factory(problem):
            return RpcTaskFlow(
                task_id=f"reg-{problem.business_id}",
                title=problem.title,
                problem_text=problem.statement,
                engine_home=work_root / "rpc-homes" / problem.business_id,
                api_key=args.api_key,
                base_url=args.base_url,
                model=args.model,
                provider=args.provider,
                extra_env=extra_env or None,
                stage_timeout=args.stage_timeout,
            )

        return factory

    sandbox = None
    if args.sandbox == "docker":
        from engine.sandbox.docker_sandbox import DockerSandbox
        from engine.sandbox.subprocess_sandbox import detect_docker_available

        if not asyncio.run(detect_docker_available()):
            sys.exit("要求 Docker 沙箱但环境不可用；请安装并启动 Docker Desktop 后重试")
        sandbox = DockerSandbox(timeout=60)

    checkpoint_root = work_root / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)

    def factory_inproc(problem):
        return FakeLLMFlow(
            task_id=f"reg-{problem.business_id}",
            title=problem.title,
            problem_text=problem.statement,
            checkpoint_path=checkpoint_root / f"{problem.business_id}.db",
            sandbox=sandbox,
            work_root=work_root / problem.business_id,
        )

    return factory_inproc


def _build_gate_policy(args: argparse.Namespace, judge_usage: dict):
    """门禁策略：autopass=基线自动通过；rubric=SP1-3 LLM 评委（与引擎同一 BYOK 通道）。

    评委在驱动进程内调用（引擎进程不感知），Key 仅内存持有、经适配器出网，
    与引擎侧首行注入同口径（DEC-011）；用量如实计入 judge_usage，评审消耗不隐藏。
    """
    if args.gate == "autopass":
        return None
    rubrics = {stage: load_rubric_for_stage(stage, RUBRICS_DIR) for stage in STAGES}
    config = ModelConfig(
        provider=args.provider or "openai-compat", base_url=args.base_url, model=args.model
    )
    keys = KeyStore()
    keys.inject(args.api_key)
    adapter = OpenAIChatAdapter(config, keys)

    async def judge_llm(messages: list[dict[str, str]], stage: str) -> dict:
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

    def record(reply: dict) -> None:
        """评委用量入账：sink 传的是 {stage, model, usage} 记录，token 在嵌套 usage 里。"""
        usage_out = reply.get("usage") or {}
        judge_usage["calls"] += 1
        judge_usage["prompt_tokens"] += int(usage_out.get("prompt_tokens", 0) or 0)
        judge_usage["completion_tokens"] += int(usage_out.get("completion_tokens", 0) or 0)

    return RubricGatePolicy(LlmRubricEvaluator(judge_llm, usage_sink=record), rubrics)


async def _run(args: argparse.Namespace) -> int:
    if args.api_key and not (args.base_url and args.model):
        sys.exit("Key 模式需要 --base-url 与 --model（引擎拒启红线：禁猜测端点）")
    problems = stratified_sample(args.per_category) if args.per_category else REGRESSION_SET
    channel = "real_key" if (args.driver == "rpc" and args.api_key) else CHANNEL_BASELINE
    if args.gate == "rubric" and channel != CHANNEL_REAL:
        sys.exit(
            "--gate rubric 需要真实 Key 通道（--driver rpc + --api-key/--base-url/--model）：\n"
            "FakeLLM 不是评委，用它评审只会得到恒定不通过并污染判定分母（DEC-024）"
        )
    print(f"[回归集] {len(problems)} 题 · 驱动={args.driver} · 通道={channel} · 门禁={args.gate}")

    # 断点恢复演练：每类题型抽 1 题，modeling 完成后 kill（SP1-7 §2 断点恢复矩阵）
    kill_plan: dict[str, str] = {}
    if not args.no_kill_resume:
        seen: set[str] = set()
        for problem in problems:
            if problem.category not in seen:
                seen.add(problem.category)
                kill_plan[problem.business_id] = "modeling"
        if kill_plan:
            print(f"[恢复演练] kill 点=modeling 后：{', '.join(kill_plan)}")

    judge_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    runner = AcceptanceRunner(
        _build_factory(args), gate_policy=_build_gate_policy(args, judge_usage), channel=channel
    )
    report = await runner.run_all(problems, kill_plan=kill_plan or None,
                                  evidence=EvidenceWriter(args.evidence))

    passed = sum(1 for r in report.results if r.passed)
    resumed = sum(1 for r in report.results if r.resumed)
    print(f"[结果] 通过 {passed}/{len(report.results)}"
          f" · 通道成功率 {report.success_rate(channel):.2%} · 恢复演练 {resumed} 题")
    if args.gate == "rubric":
        print(f"[门禁] rubric 评审 {judge_usage['calls']} 次调用"
              f" · prompt {judge_usage['prompt_tokens']} / completion "
              f"{judge_usage['completion_tokens']} tokens（评委消耗已计入，DEC-024 同口径统计）")
    print(f"[决策门] {report.decision()}")

    writer = EvidenceWriter(args.evidence)
    title = (
        "SP1-7 回归基线报告（FakeLLM 通道）"
        if channel == CHANNEL_BASELINE else "SP1-7 回归报告（真实 Key 判定通道）"
    )
    _, report_path = writer.write_summary(
        report, title=title,
        extra={
            "driver": args.driver, "sandbox": args.sandbox, "gate_mode": args.gate,
            "judge_usage": judge_usage,
        },
    )
    print(f"[证据] {args.evidence}")
    print(f"[报告] {report_path}")

    if channel == CHANNEL_BASELINE:
        # 基线护栏：确定性链路必须全绿（FakeLLM 结果仅作护栏，不计 SP1-7 判定分母）
        if report.success_rate(channel) < 1.0:
            print("[FAIL] FakeLLM 基线护栏破防：确定性链路存在失败样本，先修引擎再谈真实回归")
            return 1
        print("[PASS] FakeLLM 基线护栏全绿（真实 Key 回归窗口 11-05~11-14）")
        return 0
    verdict = report.decision().verdict
    print(f"[判定] 真实 Key 通道决策门：{verdict}（正式判定会 11-14，QA 主持）")
    return 0 if verdict == "GO" else 1


def main() -> None:
    configure_stdio()  # 驱动脚本同样按 UTF-8 输出中文报告，与 Windows 控制台码页解耦
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
