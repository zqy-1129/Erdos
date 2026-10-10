"""质量门一键入口：ruff → mypy → pytest（含覆盖率阈值）→ 各自的守护步骤。

用法（仓库根目录，引擎用仓库 .venv、服务端用 server/.venv）：
    ./.venv/Scripts/python.exe scripts/quality_gate.py                 # 引擎 + 服务端
    ./.venv/Scripts/python.exe scripts/quality_gate.py --target engine  # 只引擎
    ./.venv/Scripts/python.exe scripts/quality_gate.py --quick          # 跳全量测试，约 10 秒

--quick 只跑 lint + type + 守护步骤（不跑全量 pytest）；任一步失败即非 0 退出。
覆盖率阈值不在这里硬编码：由 engine/pyproject.toml 与 server/pyproject.toml 的 addopts 提供，
与 CI 同一份口径。解释器一律显式解析：PATH 上的 `python` 在本机是微软商店占位 stub，调用即挂死。
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE = REPO_ROOT / "engine"
SERVER = REPO_ROOT / "server"


class Step(NamedTuple):
    name: str
    args: list[str]
    cwd: Path


def _exists_python(base: Path) -> list[Path]:
    return [base / "Scripts" / "python.exe", base / "bin" / "python3", base / "bin" / "python"]


def resolve_engine_python() -> Path:
    """引擎侧解释器：脚本自己跑在哪个 venv 就用哪个。"""
    return Path(sys.executable)


def resolve_server_python() -> Path:
    """服务端解释器：优先 server/.venv，其次当前解释器（能 import fastapi 才算数）。

    单 venv 环境（CI / Linux staging）里 server/.venv 不存在，此时若当前解释器带着服务端依赖
    就直接用它；两条都不成立则快速失败并给出创建命令，绝不回落到商店 stub 或引擎 venv。
    """
    for candidate in _exists_python(SERVER / ".venv"):
        if candidate.exists():
            return candidate
    if _imports_fastapi(Path(sys.executable)):
        return Path(sys.executable)
    raise SystemExit(
        "未找到可用的服务端解释器（既没有 server/.venv，当前解释器也 import 不了 fastapi）。\n"
        "创建：uv venv server/.venv --python 3.12 && "
        "uv pip install -p server/.venv/Scripts/python.exe -e \"./server[dev]\""
    )


def _imports_fastapi(python: Path) -> bool:
    result = subprocess.run(
        [str(python), "-c", "import fastapi"], capture_output=True, check=False
    )
    return result.returncode == 0


def _engine_steps(python: Path, quick: bool) -> list[Step]:
    steps = [
        Step("engine: ruff check", [str(python), "-m", "ruff", "check", "engine", "scripts"], REPO_ROOT),
        Step(
            "engine: mypy",
            [str(python), "-m", "mypy", "--config-file", "engine/pyproject.toml", "engine"],
            REPO_ROOT,
        ),
    ]
    if not quick:
        steps.append(Step("engine: pytest（覆盖率阈值见 pyproject）", [str(python), "-m", "pytest"], ENGINE))
    steps.append(
        Step(
            "engine: 依赖边界守护",
            [
                str(python),
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


def _server_steps(python: Path, quick: bool) -> list[Step]:
    steps = [
        Step("server: ruff check", [str(python), "-m", "ruff", "check", "app", "tests", "scripts"], SERVER),
        Step("server: mypy app", [str(python), "-m", "mypy", "app"], SERVER),
    ]
    if not quick:
        steps.append(Step("server: pytest（覆盖率阈值见 pyproject）", [str(python), "-m", "pytest"], SERVER))
    steps.append(
        Step(
            "server: 契约自检（openapi 路径/信封引用/错误码）",
            [str(python), "scripts/contract_audit.py"],
            SERVER,
        )
    )
    return steps


def main() -> int:
    # 控制台默认按 ANSI 代码页编码（本机为 cp936），中文汇总会乱码；与引擎 stdio 同策略强制 UTF-8。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="质量门一键入口")
    parser.add_argument(
        "--target",
        choices=("engine", "server", "all"),
        default="all",
        help="跑哪一侧的门禁（默认 all：引擎 + 服务端）",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="跳过全量 pytest，只跑 lint + type + 守护步骤",
    )
    args = parser.parse_args()

    steps: list[Step] = []
    if args.target in ("engine", "all"):
        steps += _engine_steps(resolve_engine_python(), args.quick)
    if args.target in ("server", "all"):
        steps += _server_steps(resolve_server_python(), args.quick)

    for index, step in enumerate(steps, start=1):
        print(f"[{index}/{len(steps)}] {step.name} ...", flush=True)
        result = subprocess.run(step.args, cwd=step.cwd, check=False)
        if result.returncode != 0:
            print(f"[quality_gate] 失败：{step.name}（退出码 {result.returncode}）", file=sys.stderr, flush=True)
            return result.returncode

    scope = "lint / type / 全量测试 / 守护步骤" if not args.quick else "lint / type / 守护步骤（--quick，未跑全量测试）"
    print(f"[quality_gate] 全部通过（target={args.target}）：{scope}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
