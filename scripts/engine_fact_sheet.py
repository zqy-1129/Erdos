"""引擎侧"代码实况"机器抽取 → docs/acceptance/engine-facts.json。

用途：文档一致性审计的**唯一事实源**。文档里的每一条引擎侧断言都必须能在这里找到出处，
找不到就是文档过时或代码缺实现（两者要分开写，不许含糊）。

刻意只做机械抽取 + 少量结构化推断，不做任何"应该是这样"的补全。
"""

import ast
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ENGINE = REPO / "engine"
OUT = REPO / "docs" / "acceptance" / "engine-facts.json"


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def module_files() -> list[dict]:
    rows = []
    for path in sorted(ENGINE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        src = read(path)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)] if tree else []
        funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] if tree else []
        tests = sum(1 for n in (tree.body if tree else [])
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))
        rows.append({
            "path": str(path.relative_to(REPO)).replace("\\", "/"),
            "lines": src.count("\n") + 1,
            "classes": classes,
            "top_level_funcs": funcs,
            "test_funcs": tests,
            "imports_third_party": sorted({
                (a.name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0])
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [])
            } | {
                (n.module or "").split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module
            } - {
                "ast", "asyncio", "base64", "collections", "contextlib", "contextvars", "copy", "csv",
                "dataclasses", "datetime", "enum", "functools", "hashlib", "importlib", "inspect", "io",
                "itertools", "json", "logging", "math", "os", "pathlib", "queue", "random", "re", "shutil",
                "signal", "socket", "sqlite3", "string", "struct", "subprocess", "sys", "tempfile", "textwrap",
                "threading", "time", "types", "typing", "unicodedata", "unittest", "uuid", "warnings", "zlib",
                "concurrent", "http", "abc", "engine", "__future__",
            }),
        })
    return rows


def rpc_surface() -> dict:
    source = read(ENGINE / "ipc" / "methods.py")
    registered = re.findall(r'server\.register\("([a-z_]+)",\s*([a-z_]+)\)', source)
    handlers = {}
    for name, fn in registered:
        match = re.search(rf"async def {fn}\(params: dict\)[\s\S]*?\n    (?:async def|server\.register)", source)
        body = match.group(0) if match else ""
        handlers[name] = {
            "required_params": sorted(set(re.findall(r'params\.get\("([a-z_]+)"\)', body))),
            "raises": sorted(set(re.findall(r'raise (?:RpcError\((\w+)|(\w+Error))', body)))and sorted(
                set(x for tup in re.findall(r'raise (RpcError\((\w+)|(\w+Error))', body) for x in tup if x)
            ),
        }
    return {"registered": dict(registered), "handlers": handlers}


def contract_surface() -> dict:
    schema = json.loads(read(REPO / "contracts" / "engine-rpc.schema.json"))
    events = schema.get("events", {})
    methods = schema.get("methods", {})
    return {
        "method_names": sorted(methods) if isinstance(methods, dict) else sorted(methods),
        "event_names": sorted(events) if isinstance(events, dict) else sorted(events),
        "transport": {
            "framing": schema.get("transport", {}).get("framing"),
            "encoding": schema.get("transport", {}).get("encoding"),
            "owner": schema.get("transport", {}).get("encoding_owner"),
        },
        "method_count": len(methods) if hasattr(methods, "__len__") else None,
        "event_count": len(events) if hasattr(events, "__len__") else None,
    }


def event_emissions() -> dict:
    """代码里实际发射的事件名与位置（与契约声明对照，找出"契约有、代码不发"）。

    两种调用都要抓：字面量 `emit("tool.call", ...)` 与动态名 `emit(name, ...)`；
    跨行调用也必须命中，否则会误报"某事件零发射"——那正是本仓最常见的误判形状。
    """
    literal: dict[str, list[str]] = {}
    dynamic: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        src = read(path)
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        for match in re.finditer(r'\.emit\(\s*(?:"([a-z.]+)"|([A-Za-z_][\w.]*))', src):
            line_no = src[: match.start()].count("\n") + 1
            if match.group(1):
                literal.setdefault(match.group(1), []).append(f"{rel}:{line_no}")
            else:
                dynamic.append(f"{rel}:{line_no} emit({match.group(2)})")
    return {"literal": literal, "dynamic_name_calls": dynamic}


def env_vars() -> dict:
    """引擎与驱动脚本读取的环境变量。

    必须解析常量间接引用：`IMPL_ENV = "ERDOS_ORCH_IMPL"` 之后写 `os.environ.get(IMPL_ENV)`
    是最常见的写法，只匹配字面量会漏报——而"唯一事实源"漏一个开关，比没有事实源更危险
    （本轮就是这么漏掉 ERDOS_ORCH_IMPL 的）。
    """
    scan_roots = [ENGINE, REPO / "scripts"]
    files = [
        p
        for root in scan_roots
        for p in root.rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]
    # 第一遍：收集 NAME = "ERDOS_XXX" 形式的常量
    const_to_var: dict[str, str] = {}
    for path in files:
        for match in re.finditer(r'^([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=\s*"(ERDOS_[A-Z0-9_]+)"',
                                 read(path), re.M):
            const_to_var[match.group(1)] = match.group(2)

    used: dict[str, list[str]] = {}

    def record(var: str, path: pathlib.Path, line_no: int) -> None:
        used.setdefault(var, []).append(f"{path.relative_to(REPO)}:{line_no}")

    for path in files:
        src = read(path)
        for match in re.finditer(
            r'environ\.get\(\s*("(?P<lit>ERDOS_[A-Z0-9_]+)"|(?P<const>[A-Z][A-Z0-9_]*))'
            r'|environ\[\s*"(?P<idx>ERDOS_[A-Z0-9_]+)"'
            r'|environ\.setdefault\(\s*"(?P<sd>ERDOS_[A-Z0-9_]+)"',
            src,
        ):
            line_no = src[: match.start()].count("\n") + 1
            var = match.group("lit") or match.group("idx") or match.group("sd")
            if var is None:
                var = const_to_var.get(match.group("const") or "", "")
            if var:
                record(var, path, line_no)
    return used


def env_strip_prefixes() -> dict:
    """引擎无条件剥离的宿主环境变量前缀（隐私红线防御）。"""
    from engine._runtime_env import FRAMEWORK_ENV_PREFIXES

    return {
        "prefixes": list(FRAMEWORK_ENV_PREFIXES),
        "called_from": "engine/__init__.py（包导入期，早于任何 lang* 依赖读取 env）",
    }


def constants() -> dict:
    import engine.__main__ as entry
    from engine.orchestrator.graph import (
        CHECKPOINT_EXECUTED,
        CHECKPOINT_GATE_PASSED,
        GRAPH_CHANNELS,
        STAGES,
        gate_node_name,
    )
    from engine.orchestrator.solve_loop import LOOP_CHANNELS, MAX_DISPATCHES, MAX_REPAIRS

    return {
        "stages": list(STAGES),
        "gate_node_names": [gate_node_name(s) for s in STAGES],
        "checkpoint_status_literals": [CHECKPOINT_EXECUTED, CHECKPOINT_GATE_PASSED],
        "graph_channels": list(GRAPH_CHANNELS),
        "loop_channels": list(LOOP_CHANNELS),
        "loop_budget": {"max_repairs": MAX_REPAIRS, "max_dispatches": MAX_DISPATCHES},
        "grace_exit_seconds": entry.GRACE_EXIT_SECONDS,
        "engine_version_fallback": "0.1.0",
    }


def sandbox_facts() -> dict:
    from engine.sandbox import base as b
    from engine.sandbox import constants as c

    src = read(ENGINE / "sandbox" / "subprocess_sandbox.py")
    docker_src = read(ENGINE / "sandbox" / "docker_sandbox.py")
    return {
        "base_module_exports": [n for n in dir(b) if not n.startswith("_")],
        # 阈值单源（G32/W37）：数值取自 constants.py，"源码里是否还残留裸字面量"由文本扫描判定
        "thresholds": {
            "owner": "engine/sandbox/constants.py",
            "timeout_seconds": c.SANDBOX_TIMEOUT_SECONDS,
            "memory_limit_mb": c.SANDBOX_MEMORY_LIMIT_MB,
            "docker_memory_flag": c.DOCKER_MEMORY_FLAG,
        },
        "defaults_reference_constants": {
            "subprocess_timeout": "timeout: float = SANDBOX_TIMEOUT_SECONDS" in src,
            "subprocess_memory": "memory_limit_mb: int = SANDBOX_MEMORY_LIMIT_MB" in src,
            "docker_timeout": "timeout: float = SANDBOX_TIMEOUT_SECONDS" in docker_src,
            "docker_memory": "memory: str = DOCKER_MEMORY_FLAG" in docker_src,
        },
        "stray_numeric_defaults": re.findall(
            r"(?:timeout: float|memory_limit_mb: int|memory: str) = (?!SANDBOX_|DOCKER_)[\d\"']+",
            src + docker_src,
        ),
        "subprocess_default_timeout": re.findall(r"timeout: float = (\d+)", src),
        "memory_limit_field_present": "memory_limit_mb" in src,
        "memory_enforced_in_subprocess": bool(re.search(r"memory_limit_mb.*(?:JobObject|setrlimit|rlimit)", src, re.S)),
        "docker_memory_flag": "--memory" in docker_src,
        "unimplemented_comments": re.findall(r"# *(.*?(?:需额外实现|未实现|TODO|待实现).*)", src),
    }


def tool_facts() -> dict:
    from engine.sandbox.subprocess_sandbox import SubprocessSandbox
    from engine.tools import build_default_registry

    reg = build_default_registry(SubprocessSandbox(timeout=1))
    payloads = reg.tool_payloads()
    return {
        "tools": [
            {
                "name": p["function"]["name"],
                "params_schema": p["function"].get("parameters", {}),
                "description": p["function"].get("description", ""),
            }
            for p in payloads
        ]
    }


def provider_facts() -> dict:
    """离线能力矩阵 / 默认 provider / tool_mode 枚举 / 客户端可产出值 四方同抽。

    G31 的形状是"三个名字看起来是一回事"。只抽引擎侧无法发现漂移，必须把客户端源码里的
    可取值一起抽出来放在同一份事实里，比对才有抓手。
    """
    from dataclasses import asdict

    from engine.adapters.capabilities import (
        DEFAULT_PROVIDER,
        FIXTURE_MATRIX,
        KNOWN_PROVIDERS,
        TOOL_MODES,
    )

    source = read(REPO / "client" / "main" / "credential-registry.ts")
    entry = read(ENGINE / "__main__.py")
    return {
        "default_provider": DEFAULT_PROVIDER,
        "known_providers": list(KNOWN_PROVIDERS),
        "tool_modes": list(TOOL_MODES),
        "matrix": {p: {**asdict(c), "tool_mode": c.tool_mode} for p, c in FIXTURE_MATRIX.items()},
        "client_produced": sorted(set(re.findall(r'"([a-z0-9-]*compat)"', source))),
        "entry_uses_matrix_constants": {
            "imports_default": "DEFAULT_PROVIDER" in entry,
            "imports_tool_modes": "TOOL_MODES" in entry,
            "literal_openai_compat_in_entry": '"openai-compat"' in entry,
        },
    }


def checkpoint_schema() -> dict:
    """引擎本地三库的全部建表语句与并发参数（不能只扫 store.py）。

    之前只扫 checkpoint/store.py，导致 operations/audit_trail/artifact_index 不在"唯一事实源"
    里——文档比对拿到的是半覆盖事实源，比拿到错误结论更难发现。
    """
    sources = {
        "checkpoints.db": ENGINE / "checkpoint" / "store.py",
        "operations.db": ENGINE / "orchestrator" / "operations.py",
        "audit.db": ENGINE / "trail" / "store.py",
    }
    tables: dict[str, list[str]] = {}
    pragmas: dict[str, list[str]] = {}
    orders: dict[str, list[str]] = {}
    columns: dict[str, list[str]] = {}
    for db, path in sources.items():
        if not path.exists():
            tables[db] = [f"MISSING FILE {path}"]
            continue
        src = read(path)
        tables[db] = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", src)
        pragmas[db] = re.findall(r"PRAGMA ([a-z_]+)(?: = (\S+))?", src)
        orders[db] = re.findall(r"ORDER BY ([\w, ]+)", src)
        cols: list[str] = []
        for create in re.findall(r"CREATE TABLE IF NOT EXISTS \w+ \(([\s\S]+?)\n\s*\)", src):
            cols += [
                m.group(1)
                for m in re.finditer(r"^\s*([a-z_]+)\s+(TEXT|INTEGER)", create, re.M)
            ]
        columns[db] = cols
    literals = {}
    store_src = read(sources["checkpoints.db"])
    for name in ("CHECKPOINT_STATUS_EXECUTED", "CHECKPOINT_STATUS_GATE_PASSED"):
        match = re.search(rf'{name} = "([^"]+)"', store_src)
        literals[name] = match.group(1) if match else None
    literals["guard_fn"] = "gate_passed" if "def gate_passed" in store_src else None
    return {
        "dbs": tables,
        "columns": columns,
        "pragmas": {db: [list(t) for t in rows] for db, rows in pragmas.items()},
        "order_by": orders,
        "status_literals": literals,
        "writers_of_delete": re.findall(
            r"DELETE FROM (\w+)", "".join(read(p) for p in sources.values() if p.exists())
        ),
    }


def trail_facts() -> dict:
    """留痕面：四类事件、两套脱敏词表、是否真的只追加。"""
    src = read(ENGINE / "trail" / "store.py")
    redact_src = read(ENGINE / "trail" / "redact.py")
    recorder_src = read(ENGINE / "trail" / "recorder.py")
    return {
        "event_types": re.findall(r'^\s+[A-Z_]+ = "([a-z_]+)"', src, re.M),
        "store_uses_vocab": "TRAIL_SENSITIVE_FRAGMENTS" in src,
        "vocab_checkpoints": _list_literal(redact_src, "SENSITIVE_FRAGMENTS"),
        "vocab_trail": _list_literal(redact_src, "TRAIL_SENSITIVE_FRAGMENTS"),
        "record_methods": re.findall(r"def (record_\w+)", recorder_src),
        "delete_or_purge_api_present": bool(re.search(r"\b(DELETE FROM|DROP TABLE|purge)\b", src)),
    }


def _list_literal(src: str, name: str) -> list[str] | None:
    match = re.search(rf"{name}(?::[^=]+)?\s*=\s*[\(\[]([\s\S]*?)[\)\]]", src)
    if not match:
        return None
    return re.findall(r'"([^"]+)"', match.group(1))


def quality_numbers() -> dict:
    py = read(ENGINE / "pyproject.toml")
    return {
        "coverage_gate": re.findall(r"--cov-fail-under=(\d+)", py),
        "declared_dependencies": re.findall(r'^\s{4}"([a-zA-Z0-9_\-]+)[^"]*",\s*$', py, re.M),
        "requires_python": re.findall(r'requires-python = "([^"]+)"', py),
    }


def git_facts() -> dict:
    def run(*args):
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO), check=False,
        ).stdout.strip()

    return {
        "head": run("rev-parse", "HEAD"),
        "short": run("log", "--oneline", "-1"),
        "ahead_of_origin": run("rev-list", "--count", "origin/develop..develop"),
    }


def main() -> None:
    facts: dict[str, object] = {}
    safe = {
        "git": git_facts,
        "constants": constants,
        "contract": contract_surface,
        "rpc": rpc_surface,
        "event_emissions": event_emissions,
        "env_vars": env_vars,
        "env_strip": env_strip_prefixes,
        "checkpoint": checkpoint_schema,
        "trail": trail_facts,
        "sandbox": sandbox_facts,
        "tools": tool_facts,
        "providers": provider_facts,
        "quality": quality_numbers,
        "modules": module_files,
    }
    for key, fn in safe.items():
        try:
            facts[key] = fn()
        except Exception as exc:  # noqa: BLE001 - 抽取失败必须显式记录而不是缺项
            facts[key] = {"ERROR": f"{type(exc).__name__}: {exc}"}
    facts["provenance"] = {
        "generated_by": "scripts/engine_fact_sheet.py",
        "recompute": ".venv/Scripts/python.exe scripts/engine_fact_sheet.py",
        "head": (facts.get("git") or {}).get("head") if isinstance(facts.get("git"), dict) else None,
        "note": "本文档面的唯一事实源；分区带 ERROR 键即抽取失败，不得据此下结论",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[engine-facts] 写入 {OUT} （{OUT.stat().st_size} bytes）")
    errs = [k for k, v in facts.items() if isinstance(v, dict) and "ERROR" in v]
    print("[engine-facts] 抽取失败的分区:", errs or "无")


if __name__ == "__main__":
    main()
