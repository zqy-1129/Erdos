"""SP1-7 FakeLLM 全量回归一键驱动（验收方案步骤 2：基线护栏，不计入判定分母）。

用法（仓库根目录）：
    python scripts/run_regression.py                      # 全量 20 题 + 每类 kill/恢复演练 1 题
    python scripts/run_regression.py --per-category 1     # 每类抽 1 题快速回归
    python scripts/run_regression.py --sandbox docker     # Docker 沙箱模式（不可用即拒启）
    python scripts/run_regression.py --no-kill-resume     # 关闭断点恢复演练

流程：分层抽样 → 逐题 task 流四阶段（FakeLLM 离线确定性）→ 证据落盘 → 基线报告。
产物：docs/acceptance/sp1-7/{business_id}.json + summary.json + report.md。

护栏语义：FakeLLM 通道成功率必须 100%（确定性链路破防即回归失败，退出码 1）；
真实 Key 通道（判定通道）走 run_paper_e2e.py / RPC 驱动，窗口 11-05~11-14。
"""

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine.regression.evidence import EvidenceWriter
from engine.regression.flow import FakeLLMFlow
from engine.regression.problems import REGRESSION_SET, stratified_sample
from engine.regression.runner import (
    CHANNEL_BASELINE,
    AcceptanceRunner,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SP1-7 FakeLLM 全量回归一键驱动")
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
    return parser.parse_args()


def _build_factory(args: argparse.Namespace):
    """任务流工厂（依赖注入：沙箱模式 / 工作目录 / 检查点位置）。"""
    import tempfile

    sandbox = None
    if args.sandbox == "docker":
        from engine.sandbox.docker_sandbox import DockerSandbox
        from engine.sandbox.subprocess_sandbox import detect_docker_available

        if not asyncio.run(detect_docker_available()):
            sys.exit("要求 Docker 沙箱但环境不可用；请安装并启动 Docker Desktop 后重试")
        sandbox = DockerSandbox(timeout=60)

    work_root = Path(args.work_root) if args.work_root else Path(tempfile.mkdtemp(prefix="erdos-reg-"))
    checkpoint_root = work_root / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    print(f"[工作目录] {work_root}")

    def factory(problem):
        return FakeLLMFlow(
            task_id=f"reg-{problem.business_id}",
            title=problem.title,
            problem_text=problem.statement,
            checkpoint_path=checkpoint_root / f"{problem.business_id}.db",
            sandbox=sandbox,
            work_root=work_root / problem.business_id,
        )

    return factory


async def _run(args: argparse.Namespace) -> int:
    problems = stratified_sample(args.per_category) if args.per_category else REGRESSION_SET
    print(f"[回归集] {len(problems)} 题（题型分布见证据 summary）")

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

    runner = AcceptanceRunner(_build_factory(args), channel=CHANNEL_BASELINE)
    report = await runner.run_all(problems, kill_plan=kill_plan or None,
                                  evidence=EvidenceWriter(args.evidence))

    baseline_rate = report.success_rate(CHANNEL_BASELINE)
    resumed = sum(1 for r in report.results if r.resumed)
    print(f"[结果] 通过 {sum(1 for r in report.results if r.passed)}/{len(report.results)}"
          f" · FakeLLM 成功率 {baseline_rate:.2%} · 恢复演练 {resumed} 题")
    print(f"[决策门] {report.decision()}")

    writer = EvidenceWriter(args.evidence)
    _, report_path = writer.write_summary(report, title="SP1-7 回归基线报告（FakeLLM 通道）")
    print(f"[证据] {args.evidence}")
    print(f"[报告] {report_path}")

    # 基线护栏：确定性链路必须全绿（FakeLLM 结果仅作护栏，不计 SP1-7 判定分母）
    if baseline_rate < 1.0:
        print("[FAIL] FakeLLM 基线护栏破防：确定性链路存在失败样本，先修引擎再谈真实回归")
        return 1
    print("[PASS] FakeLLM 基线护栏全绿（真实 Key 回归窗口 11-05~11-14）")
    return 0


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
