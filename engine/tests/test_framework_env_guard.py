"""宿主 env 消毒守护（P0 隐私缓解）。

缺陷形状：引擎依赖树里的 langsmith 由 **环境变量**决定是否把每次图执行的输入/输出
POST 到外部端点，而阶段产出含题面与论文正文。客户端拉起引擎时原样继承 process.env
（`client/main/engine-host/host.ts:191`），所以"用户机恰好设过该变量"就等于题面外泄。

这里锁三件事：
1. 剥离函数按前缀工作、可重复调用（纯函数，不依赖 import 顺序）；
2. 真实子进程行为——带 `LANGCHAIN_TRACING_V2=true` 拉起时，import engine 之后追踪判定为 False，
   而**不 import engine 时为 True**（对照组证明是本守护起作用，而非环境本来就没开）；
3. `engine/__init__.py` 模块级确实调用了它（防后续重构把它挪进 main() 而失去时机）。
"""

import ast
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

from engine._runtime_env import FRAMEWORK_ENV_PREFIXES, sanitize_framework_env

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD_ENV_EXCLUDED = ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "PYTHONSTARTUP")
_CHILD_ENV_EXCLUDED_PREFIXES = ("COV_CORE_", "ERDOS_")
_TRACING_ENV = {"LANGCHAIN_TRACING_V2": "true", "LANGCHAIN_API_KEY": "lsv2-fake-for-guard"}


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    """纯净子进程环境：剥离覆盖率钩子与 ERDOS_*，再叠加显式覆盖项。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CHILD_ENV_EXCLUDED and not k.startswith(_CHILD_ENV_EXCLUDED_PREFIXES)
    }
    env.update(extra)
    return env


def _run_python(code: str, extra_env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=_child_env(extra_env),
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ----------------------------------------------------------------------
# 纯函数层
# ----------------------------------------------------------------------
def test_sanitize_removes_every_lang_namespace_key() -> None:
    environ = {
        "LANGCHAIN_TRACING_V2": "true",
        "LANGCHAIN_API_KEY": "sk-lc",
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "LANGSMITH_PROJECT": "erdos",
        "PATH": "/keep/me",
        "ERDOS_MODEL_BASE_URL": "https://keep.example/v1",
    }
    removed = sanitize_framework_env(environ)

    assert sorted(removed) == [
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_PROJECT",
        "LANGSMITH_TRACING",
    ]
    # 非框架变量一律不动：BYOK 端点坐标与 PATH 不能被误剥
    assert environ["PATH"] == "/keep/me"
    assert environ["ERDOS_MODEL_BASE_URL"] == "https://keep.example/v1"


def test_sanitize_is_idempotent_and_empty_on_clean_env() -> None:
    environ = {"PATH": "/x"}
    assert sanitize_framework_env(environ) == []
    assert sanitize_framework_env(environ) == []


def test_prefix_list_covers_tracing_switches_referenced_by_langsmith() -> None:
    """判据来自 langsmith.utils.get_env_var(namespaces=("LANGSMITH","LANGCHAIN")) 的命名空间。"""
    assert set(FRAMEWORK_ENV_PREFIXES) == {"LANGCHAIN", "LANGSMITH"}


# ----------------------------------------------------------------------
# 行为层（真实子进程；证明守护改变了框架判定，而不是本来就不会开）
# ----------------------------------------------------------------------
_requires_langsmith = pytest.mark.skipif(
    find_spec("langsmith") is None,
    reason="langsmith 不在当前解释器（P7 摘除依赖后本组用例作废，由零 lang* import 守护接替）",
)

_PROBE = (
    "from langsmith.utils import tracing_is_enabled;"
    "print(tracing_is_enabled())"
)
# 走真实加载链：`python -m engine` 先执行父包 __init__（消毒），随后 __main__ 才把
# orchestrator.graph → langgraph → langsmith 拉起来。只 import engine 测不出这条链。
_PROBE_WITH_ENGINE = (
    "import engine, engine.orchestrator.graph, engine.__main__;"
    "from langsmith.utils import tracing_is_enabled;"
    "print(tracing_is_enabled())"
)


@_requires_langsmith
def test_control_group_tracing_is_on_without_engine() -> None:
    """对照组：不带 engine 时该 env 确实会打开追踪——否则下一条断言没有对照基准。"""
    assert _run_python(_PROBE, _TRACING_ENV) == "True"


@_requires_langsmith
def test_importing_engine_disables_framework_tracing() -> None:
    """框架已加载后追踪判定仍为 False：证明消毒发生在 langsmith 首次读取 env 之前。"""
    assert _run_python(_PROBE_WITH_ENGINE, _TRACING_ENV) == "False"


@_requires_langsmith
def test_engine_env_still_carries_byok_coordinates_after_sanitize() -> None:
    """消毒只动 lang*：BYOK 必需的环境变量必须原样可读（红线：不得猜测默认厂商端点）。"""
    code = (
        "import engine, os;"
        "print(os.environ.get('ERDOS_MODEL_BASE_URL'))"
    )
    env = dict(_TRACING_ENV)
    env["ERDOS_MODEL_BASE_URL"] = "https://vendor.example/v1"
    assert _run_python(code, env) == "https://vendor.example/v1"


# ----------------------------------------------------------------------
# 结构层：调用时机不可退化
# ----------------------------------------------------------------------
def test_package_init_sanitizes_at_module_level() -> None:
    """`python -m engine` 会先执行父包 __init__；放进 main() 就来不及（lru_cache 已缓存）。"""
    tree = ast.parse((REPO_ROOT / "engine" / "__init__.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in tree.body  # 只看模块级语句，不看函数体内调用
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "sanitize_framework_env"
    ]
    assert len(calls) == 1, "engine/__init__.py 必须且只需在模块级调用一次消毒"

    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    first_import_line = min((getattr(n, "lineno", 10**9) for n in imports), default=0)
    assert calls[0].lineno > first_import_line  # 消毒在 import 之后（import 自身不读 env）
    assert calls[0].lineno > 0
