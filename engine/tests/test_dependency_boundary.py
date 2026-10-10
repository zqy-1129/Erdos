"""依赖边界守护：LangChain/LangGraph 只允许出现在 engine/，引擎与云端互不 import。

为什么存在：server/client 一旦直接 import 编排框架，桌面包体积与离线红线同时失守；engine 一旦
import server/app，就绕过了 contracts/engine-rpc.schema.json 契约直连云端业务（不变量 6）。
违规怎么修：把越界能力挪回正确一侧——UI/服务端要编排就调引擎 RPC，引擎要云端数据由服务端经 RPC
响应下发；共享逻辑放进 contracts 或以参数注入，而不是跨目录 import。
"""

import ast
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 虚拟环境与构建产物：既非本仓源码，也会让整仓扫描慢一个数量级
SKIP_DIR_NAMES = {".venv", "node_modules", ".git", "dist", "__pycache__", "build"}
# 编排框架顶层包（含 langchain_core / langgraph_pre 等衍生包）
LLM_FRAMEWORKS = ("langchain", "langgraph")
# 云端/桌面侧顶层包：引擎不得依赖
CLOUD_TOP_LEVELS = frozenset({"server", "client", "app"})
ENGINE_DIR = "engine"
# 反向依赖同样越界的两侧
CLOUD_DIRS = frozenset({"server", "client"})


def _python_files(root: Path) -> list[Path]:
    """root 下待检 .py 清单（跳过虚拟环境与构建产物目录）。"""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        files.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return sorted(files)


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    """文件内绝对 import 的点分模块名与行号；相对 import（level>0）不跨顶层包，跳过。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
    return found


def _is_llm_framework(top: str) -> bool:
    return any(top == name or top.startswith(f"{name}_") for name in LLM_FRAMEWORKS)


def scan(root: Path) -> list[str]:
    """扫描仓库根 root 的源码，返回越界依赖描述（``文件:行号 规则N 说明``）；空列表即边界干净。

    root 可注入，便于用临时目录验证扫描器本身真的会报错（正向用例对 REPO_ROOT 调用）。
    """
    violations: list[str] = []
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        area = rel.split("/", 1)[0]
        in_engine = area == ENGINE_DIR
        for module, lineno in _imported_modules(path):
            top = module.split(".", 1)[0]
            site = f"{rel}:{lineno}"
            if _is_llm_framework(top) and not in_engine:
                violations.append(
                    f"{site} 规则1：LangChain/LangGraph（{module}）只能在 {ENGINE_DIR}/ 内使用，"
                    f"此处位于 {area}/"
                )
            if in_engine and top in CLOUD_TOP_LEVELS:
                violations.append(
                    f"{site} 规则2：{ENGINE_DIR}/ 不得 import 云端/桌面侧模块（{module}），"
                    f"引擎不直连云端业务代码"
                )
            if area in CLOUD_DIRS and top == ENGINE_DIR:
                violations.append(
                    f"{site} 规则3：{area}/ 不得反向 import {ENGINE_DIR}，跨端只能走 RPC 契约"
                )
    return violations


def test_repo_dependency_boundary_is_clean() -> None:
    violations = scan(REPO_ROOT)
    assert not violations, "依赖边界越界：\n" + "\n".join(violations)


def test_scan_catches_llm_framework_import_outside_engine(tmp_path: Path) -> None:
    """负向用例：临时副本里把 langgraph 放进 server/ 必须被抓到，且违规文件名出现在消息中。"""
    leak = tmp_path / "server" / "leak.py"
    leak.parent.mkdir(parents=True)
    leak.write_text("from langgraph.graph import StateGraph\n", encoding="utf-8")
    allowed = tmp_path / "engine" / "orchestrator.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("from langgraph.graph import StateGraph\n", encoding="utf-8")
    vendored = tmp_path / ".venv" / "lib" / "site-packages" / "langgraph" / "__init__.py"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("import langgraph\n", encoding="utf-8")

    violations = scan(tmp_path)

    assert len(violations) == 1, violations
    assert "server/leak.py:1" in violations[0], violations[0]
    assert "langgraph.graph" in violations[0], violations[0]


def test_scan_catches_engine_and_cloud_mutual_imports(tmp_path: Path) -> None:
    engine_side = tmp_path / "engine" / "cloud_call.py"
    cloud_side = tmp_path / "server" / "run_engine.py"
    for path in (engine_side, cloud_side):
        path.parent.mkdir(parents=True, exist_ok=True)
    engine_side.write_text("from server.app.core.config import settings\n", encoding="utf-8")
    cloud_side.write_text("import engine.orchestrator\n", encoding="utf-8")

    violations = scan(tmp_path)

    assert len(violations) == 2, violations
    joined = "\n".join(violations)
    assert "规则2" in joined and "engine/cloud_call.py:1" in joined, joined
    assert "规则3" in joined and "server/run_engine.py:1" in joined, joined
