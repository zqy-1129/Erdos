"""引擎质量门一键入口：ruff → mypy → pytest（含覆盖率阈值）→ 依赖边界守护。

用法（仓库根目录）：./.venv/Scripts/python.exe scripts/quality_gate.py [--quick]
--quick 跳过全量 pytest（约 100 秒），只跑 lint + type + 依赖边界守护；任一步失败即非 0 退出。
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE = REPO_ROOT / "engine"
# 解释器一律取 sys.executable：PATH 上的 `python` 在本机是微软商店占位 stub，调用即挂死。
PYTHON = sys.executable


class Step(NamedTuple):
    name: str
    args: list[str]
    cwd: Path


def _steps(quick: bool) -> list[Step]:
    """按 CI 同序组装门禁步骤；覆盖率阈值由 engine/pyproject.toml addopts 提供，不在此硬编码。"""
    steps = [
        Step("ruff check", [PYTHON, "-m", "ruff", "check", "engine", "scripts"], REPO_ROOT),
        Step(
            "mypy",
            [PYTHON, "-m", "mypy", "--config-file", "engine/pyproject.toml", "engine"],
            REPO_ROOT,
        ),
    ]
    if not quick:
        steps.append(Step("pytest (coverage >= 85%)", [PYTHON, "-m", "pytest"], ENGINE))
    steps.append(
        Step(
            "dependency boundary guard",
            [
                PYTHON,
                "-m",
                "pytest",
                "tests/test_dependency_boundary.py",
                "--no-cov",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            ENGINE,
        )
    )
    return steps


def main() -> int:
    # 控制台默认按 ANSI 代码页编码（本机为 cp936），中文汇总会乱码；与引擎 stdio 同策略强制 UTF-8。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="引擎质量门一键入口")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="跳过全量 pytest，只跑 lint + type + 依赖边界守护",
    )
    args = parser.parse_args()

    steps = _steps(args.quick)
    for index, step in enumerate(steps, start=1):
        print(f"[{index}/{len(steps)}] {step.name} ...", flush=True)
        result = subprocess.run(step.args, cwd=step.cwd, check=False)
        if result.returncode != 0:
            print(
                f"[quality_gate] 失败：{step.name}（退出码 {result.returncode}）",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode

    scope = "lint / type / 全量测试 / 依赖边界守护"
    if args.quick:
        scope = "lint / type / 依赖边界守护（--quick，未跑全量测试）"
    print(f"[quality_gate] 全部通过：{scope}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
