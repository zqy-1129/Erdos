"""EN-WIRE（W2）进程级验收：`python -m engine` 真实装配链路。

覆盖（对应《任务执行手册》W2 验收）：
- 无 Key 模式端到端：首行空行 → start_stage 后台驱动编排器 → stage.progress 事件回流；
- 首行密钥约定：合法 JSON-RPC 首行回放执行（兼容无密钥调用方）；
- Key 模式拒启红线：注入 Key 但缺 ERDOS_MODEL_BASE_URL → 退出码 2 + 可读诊断；
- 缺 ERDOS_ENGINE_HOME → 拒启退出码 2；
- SIGTERM（Windows 走 CTRL_BREAK）优雅退出，退出码 0。

FakeLLM 为确定性假模型（测试/演示专用）；本文件不产生真实模型调用。
"""

import ast
import inspect
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from engine.adapters.capabilities import CapabilityCache, ProviderCapabilities, capability_key

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_HOME_KEY = "ERDOS_ENGINE_HOME"
# 事件负载禁止出现的敏感字段名（trail/事件红线，DEC-011）
FORBIDDEN_EVENT_FIELDS = {"api_key", "authorization", "secret", "token", "key"}
# 子进程必须剥离的钩子：覆盖率钩子一旦在引擎进程内自启动（pytest-cov 经 COV_CORE_SOURCE
# 下发），其导入期告警会按宿主码页（Windows 常为 cp936）写进 stderr，把 UTF-8 协议管道搅成
# 混合字节序——那时引擎自己的 configure_stdio() 还没跑到，无法纠正。进程级测试只测协议。
_CHILD_ENV_EXCLUDED = ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "PYTHONSTARTUP")
_CHILD_ENV_EXCLUDED_PREFIXES = ("COV_CORE_",)


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造引擎子进程环境：剥离 ERDOS_* 与覆盖率/启动钩子，再叠加显式覆盖项。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ERDOS_")
        and k not in _CHILD_ENV_EXCLUDED
        and not k.startswith(_CHILD_ENV_EXCLUDED_PREFIXES)
    }
    if extra:
        env.update(extra)
    return env


def _spawn(
    lines: list[str],
    home: Path | None = None,
    close_stdin: bool = True,
    drop_home: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """拉起引擎进程；返回 Popen（stdout 由调用方经队列读取）。"""
    env = _child_env(extra_env)
    if home is not None:
        env[ENGINE_HOME_KEY] = str(home)
    if drop_home:
        env.pop(ENGINE_HOME_KEY, None)
    creationflags = 0
    if sys.platform == "win32":  # 平台守卫用 sys.platform：mypy 静态收窄仅识别它（os.name 不参与）
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(  # noqa: S603 - 固定解释器与模块名，测试受控输入
        [sys.executable, "-m", "engine"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=env,
        creationflags=creationflags,
    )
    assert proc.stdin is not None
    for line in lines:
        proc.stdin.write(line + "\n")
    proc.stdin.flush()
    if close_stdin:
        proc.stdin.close()
    return proc


def _reader(proc: subprocess.Popen) -> queue.Queue:
    """后台线程逐行读 stdout；EOF 后投递哨兵 None。"""
    out: queue.Queue = queue.Queue()
    assert proc.stdout is not None

    def run() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            out.put(line.rstrip("\n"))
        out.put(None)

    threading.Thread(target=run, daemon=True).start()
    return out


def _close_pipes(proc: subprocess.Popen) -> None:
    """显式关闭管道句柄，避免父进程 GC 产生 ResourceWarning。"""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _drain(q: queue.Queue, timeout: float) -> list[str]:
    """读取至 EOF 或超时；返回全部行（stdout 关闭后自然结束）。"""
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        lines.append(line)
    return lines


def _wait_for_response(q: queue.Queue, req_id: str, timeout: float = 30.0) -> dict:
    """阻塞读取直到出现指定 id 的响应行（保持 stdin 打开的场景用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        msg = json.loads(line)
        if msg.get("id") == req_id and ("result" in msg or "error" in msg):
            return msg
    raise AssertionError(f"超时未收到响应：{req_id}")


def test_no_key_end_to_end(tmp_path: Path) -> None:
    """无 Key 模式：空行首行 → start_stage → stage.progress 事件回流 + get_status 可查。"""
    proc = _spawn(
        [
            "",  # 首行空行 → 无 Key 模式（FakeLLM）
            json.dumps({"jsonrpc": "2.0", "id": "ui-1", "method": "start_stage",
                        "params": {"task_id": "t1", "stage": "analysis"}}),
            json.dumps({"jsonrpc": "2.0", "id": "ui-2", "method": "get_status", "params": {}}),
        ],
        home=tmp_path / "home",
    )
    q = _reader(proc)
    lines = _drain(q, timeout=60)
    proc.wait(timeout=30)
    _close_pipes(proc)

    responses = [json.loads(x) for x in lines if '"id"' in x and '"event"' not in x]
    events = [json.loads(x) for x in lines if '"event"' in x]
    by_id = {r["id"]: r for r in responses}

    assert by_id["ui-1"]["result"]["status"] == "running"
    assert by_id["ui-2"]["result"]["engine"] in ("running", "idle")
    progress = [e for e in events if e["event"] == "stage.progress" and e["task_id"] == "t1"]
    assert [p["progress"] for p in progress] == [0.05, 1.0], progress
    assert all(p["stage"] == "analysis" for p in progress)
    # 事件负载无敏感字段（trail/事件红线）
    assert all(not (set(e) & FORBIDDEN_EVENT_FIELDS) for e in events)
    assert proc.returncode == 0


def test_first_line_jsonrpc_replayed(tmp_path: Path) -> None:
    """首行为合法 JSON-RPC 请求行 → 无 Key 模式 + 该行回放执行（兼容无密钥调用方）。"""
    status_req = json.dumps({"jsonrpc": "2.0", "id": "ui-1", "method": "get_status", "params": {}})
    proc = _spawn([status_req, status_req.replace("ui-1", "ui-2")], home=tmp_path / "home")
    q = _reader(proc)
    lines = _drain(q, timeout=30)
    proc.wait(timeout=30)
    _close_pipes(proc)

    responses = [json.loads(x) for x in lines if '"id"' in x and '"event"' not in x]
    assert [r["id"] for r in responses] == ["ui-1", "ui-2"]
    assert all("engine" in r["result"] for r in responses)
    assert proc.returncode == 0


BASE_URL = "https://api.test/v1"
MODEL = "reasoner-mini"


def test_cached_capabilities_drive_tool_mode(tmp_path: Path) -> None:
    """EN-CAP 消费侧接线：capabilities.json 的实测能力决定装配期 tool_mode。

    装配期不做网络探测（冷启动 <2s 硬目标）——真实探测由 provider_test 落盘，这里验证
    "落盘 → 下次拉起即被复用"这条闭环，并确认 initialize 如实上报。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True)
    CapabilityCache(home).store(
        capability_key(BASE_URL, MODEL),
        ProviderCapabilities(tools=True, tool_choice=True, models_endpoint=True,
                             probe_source="probe"),
    )
    proc = _spawn(
        [
            "sk-dummy-key-1234567890",
            json.dumps({"jsonrpc": "2.0", "id": "i-1", "method": "initialize",
                        "params": {"client_protocol_version": 2}}),
        ],
        home=home,
        extra_env={
            "ERDOS_MODEL_BASE_URL": BASE_URL,
            "ERDOS_MODEL_NAME": MODEL,
            "ERDOS_MODEL_PROVIDER": "unknown-provider",  # 不在离线矩阵里，只能来自缓存
        },
    )
    q = _reader(proc)
    msg = _wait_for_response(q, "i-1")
    proc.stdin.close()  # type: ignore[union-attr]
    proc.wait(timeout=30)
    _close_pipes(proc)

    assert msg["result"]["capabilities"]["tool_mode"] == "tool_loop"


def test_protocol_streams_are_utf8_regardless_of_console_codepage(tmp_path: Path) -> None:
    """NDJSON 字节序红线：引擎输出/输入恒为 UTF-8，即使宿主码页是 GBK。

    Windows 控制台默认码页常为 cp936/GBK，中文负载（题面、门禁意见）不强制编码就会按 GBK
    落管道，主进程按 utf8 解码即得乱码并静默丢弃事件——Linux CI 永远不会暴露这条。
    用 PYTHONIOENCODING=gbk 把子进程置于 GBK 宿主下，验证引擎自行纠偏。
    """
    create = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "ui-1",
            "method": "task_create",
            "params": {
                "task_id": "t1",
                "title": "装配误差优化",
                "problem_text": "某厂需确定最优生产计划，使装配误差最小。",
            },
        }
    )
    proc = _spawn(
        ["", create, create.replace("ui-1", "ui-2")],  # 第二次同 task_id → 中文冲突诊断
        home=tmp_path / "home",
        extra_env={"PYTHONIOENCODING": "gbk"},
    )
    lines = _drain(_reader(proc), timeout=30)
    proc.wait(timeout=30)
    _close_pipes(proc)

    assert lines, "引擎 stdout 按 UTF-8 读取无一行有效——协议字节被宿主码页污染"
    responses = {json.loads(x)["id"]: json.loads(x) for x in lines if '"id"' in x and '"event"' not in x}
    assert responses["ui-1"]["result"]["status"] == "created"
    assert "任务已登记" in responses["ui-2"]["error"]["message"]


def test_key_without_model_config_rejected(tmp_path: Path) -> None:
    """Key 模式缺 ERDOS_MODEL_BASE_URL/NAME → 退出码 2 + 可读诊断（禁止猜默认厂商）。"""
    proc = _spawn(["sk-dummy-key-1234567890"], home=tmp_path / "home")
    _reader(proc)  # 防塞死保护
    stderr = proc.stderr.read() if proc.stderr else ""
    proc.wait(timeout=10)
    _close_pipes(proc)
    assert proc.returncode == 2
    assert "ERDOS_MODEL_BASE_URL" in stderr


def test_missing_home_rejected() -> None:
    """缺 ERDOS_ENGINE_HOME → 拒启退出码 2 + 可读诊断（不创建野目录）。"""
    proc = _spawn([], home=None, drop_home=True)
    stderr = proc.stderr.read() if proc.stderr else ""
    proc.wait(timeout=10)
    _close_pipes(proc)
    assert proc.returncode == 2
    assert "ERDOS_ENGINE_HOME" in stderr


def test_version_flag_prints_and_exits() -> None:
    """W17 打包前置：`engine --version` 打印版本退出码 0，无需 ERDOS_ENGINE_HOME/stdin。"""
    env = _child_env()
    for flag in ("--version", "-V"):
        proc = subprocess.run(  # noqa: S603 - 固定解释器/模块/受控参数
            [sys.executable, "-m", "engine", flag],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, f"{flag} 应退出码 0，stderr={proc.stderr}"
        assert re.match(r"^\d+\.\d+\.\d+$", proc.stdout.strip()), f"{flag} 应输出语义化版本，got={proc.stdout!r}"


def test_sigterm_graceful_exit(tmp_path: Path) -> None:
    """SIGTERM（Windows 走 CTRL_BREAK）→ 协作停机，退出码 0，300ms 硬上限内。"""
    proc = _spawn(
        [json.dumps({"jsonrpc": "2.0", "id": "ui-1", "method": "get_status", "params": {}})],
        home=tmp_path / "home",
        close_stdin=False,  # 保持 stdin 打开，避免 EOF 自然退出抢在信号前
    )
    q = _reader(proc)
    _wait_for_response(q, "ui-1", timeout=30)
    if sys.platform == "win32":  # 平台守卫用 sys.platform（同上：Linux CI mypy 需静态收窄）
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
    _close_pipes(proc)
    assert proc.returncode == 0


# ----------------------------------------------------------------------
# EC-D5 启动自检：本地库损坏 → 诊断式拒启（保留现场，不静默重建）
# ----------------------------------------------------------------------
def test_verify_stores_healthy_and_corrupt(tmp_path: Path, capsys) -> None:
    """单元级：空目录/健康库通过；损坏库 SystemExit(2) + 可读诊断。"""
    import sqlite3

    from engine.__main__ import _verify_stores

    _verify_stores(tmp_path)  # 首次启动：无库文件，放行（由各存储构造器初始化）
    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    _verify_stores(tmp_path)  # 健康库 quick_check=ok

    (tmp_path / "checkpoints.db").write_bytes(b"garbage not a sqlite database file")
    with pytest.raises(SystemExit) as ei:
        _verify_stores(tmp_path)
    assert ei.value.code == 2
    assert "损坏" in capsys.readouterr().err
    assert (tmp_path / "checkpoints.db").exists()  # 保留现场：不删除/不重建


def test_startup_corrupt_store_fails_closed(tmp_path: Path) -> None:
    """进程级：损坏库上拉起引擎 → 退出码 2 + stderr 诊断（不进入服务循环）。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "audit.db").write_bytes(b"corrupted payload, definitely not sqlite")
    proc = _spawn([], home=home)
    proc.wait(timeout=15)
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    _close_pipes(proc)
    assert proc.returncode == 2
    assert "损坏" in stderr


# ----------------------------------------------------------------------
# W40 · EN-BOOT-DIAG（G38/G39）：启动期异常必须是可读诊断 + 诊断码 2，不是挂死或 traceback
# ----------------------------------------------------------------------
class _BlockingStdin:
    """readline() 永不返回的假 stdin（模拟宿主忘了写首行）。"""

    def __init__(self) -> None:
        self._gate = threading.Event()

    def readline(self) -> str:  # noqa: ANN001 - 与 sys.stdin 同签名
        self._gate.wait(30)
        return ""


def test_first_line_timeout_refuses_startup(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """G38：首行握手超时 → 退出码 2 + EC-T6 诊断；不得静默当作"无 Key"继续跑。"""
    from engine.__main__ import _read_first_line

    monkeypatch.setattr(sys, "stdin", _BlockingStdin())
    with pytest.raises(SystemExit) as caught:
        _read_first_line(timeout=0.2)
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "首行握手" in err and "EC-T6" in err, err


def test_first_line_timeout_matches_ec_t6_clause() -> None:
    """EC-T6 写的是 10s；常量漂移就等于条款失效，所以直接钉字面值。"""
    from engine.__main__ import FIRST_LINE_TIMEOUT_SECONDS

    assert FIRST_LINE_TIMEOUT_SECONDS == 10.0


def test_first_line_eof_is_no_key_not_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOF（宿主关管道）与超时是两条路径：前者返回空串走无 Key，后者必须拒启。"""
    from engine.__main__ import _read_first_line

    class _EofStdin:
        def readline(self) -> str:
            return ""

    monkeypatch.setattr(sys, "stdin", _EofStdin())
    assert _read_first_line(timeout=0.5) == ""


def test_sandbox_unavailable_maps_to_diagnostic_exit(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """G39：Docker 强制模式下沙箱不可用 → 退出码 2 + 原因，而不是 traceback + 退出码 1。"""
    import engine.__main__ as entry
    from engine.sandbox.base import SandboxUnavailableError

    async def _unavailable() -> None:
        raise SandboxUnavailableError("镜像缺失：erdos-sandbox:latest")

    monkeypatch.setattr(entry, "make_sandbox", _unavailable)
    with pytest.raises(SystemExit) as caught:
        entry._make_sandbox()
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "沙箱不可用" in err and "镜像缺失" in err, err


def test_main_uses_the_startup_guards() -> None:
    """守护装配本身：main() 必须经 `_make_sandbox()` 与带超时的 `_read_first_line()`。

    两个守卫函数单测全绿、`main()` 里却仍写裸调用，是本项目反复出现的"修了没人接"形状。
    """
    import engine.__main__ as entry

    tree = ast.parse(inspect.getsource(entry.main))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_make_sandbox" in calls, "main() 仍直接 asyncio.run(make_sandbox())"
    assert "_read_first_line" in calls
    assert "make_sandbox" not in calls
