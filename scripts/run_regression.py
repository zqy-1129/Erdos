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

通道语义（DEC-024）：--driver inproc（默认）与无 Key rpc 均为 FakeLLM 护栏通道，
不计入 ≥85% 判定分母；真实 Key 通道结果以 CHANNEL_REAL 登记，decision() 出 Go/No-Go。
产物：docs/acceptance/sp1-7/{business_id}.json + summary.json + report.md。
护栏语义：FakeLLM 通道成功率必须 100%，否则退出码 1（确定性链路破防）。
"""

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine.ipc.stdio import configure_stdio
from engine.regression.evidence import EvidenceWriter
from engine.regression.flow import FakeLLMFlow
from engine.regression.problems import REGRESSION_SET, stratified_sample
from engine.regression.runner import (
    CHANNEL_BASELINE,
    AcceptanceRunner,
)


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


async def _run(args: argparse.Namespace) -> int:
    if args.api_key and not (args.base_url and args.model):
        sys.exit("Key 模式需要 --base-url 与 --model（引擎拒启红线：禁猜测端点）")
    problems = stratified_sample(args.per_category) if args.per_category else REGRESSION_SET
    channel = "real_key" if (args.driver == "rpc" and args.api_key) else CHANNEL_BASELINE
    print(f"[回归集] {len(problems)} 题 · 驱动={args.driver} · 通道={channel}")

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

    runner = AcceptanceRunner(_build_factory(args), channel=channel)
    report = await runner.run_all(problems, kill_plan=kill_plan or None,
                                  evidence=EvidenceWriter(args.evidence))

    passed = sum(1 for r in report.results if r.passed)
    resumed = sum(1 for r in report.results if r.resumed)
    print(f"[结果] 通过 {passed}/{len(report.results)}"
          f" · 通道成功率 {report.success_rate(channel):.2%} · 恢复演练 {resumed} 题")
    print(f"[决策门] {report.decision()}")

    writer = EvidenceWriter(args.evidence)
    _, report_path = writer.write_summary(report, title="SP1-7 回归基线报告（FakeLLM 通道）")
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
