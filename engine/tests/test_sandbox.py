"""沙箱执行器单元测试（SP1-4）：正常执行 / 超时强杀 / 逃逸防护 / 环境隔离 / 执行体解析。"""

import sys
import time
from pathlib import Path

from engine.sandbox.base import validate_artifact_path
from engine.sandbox.subprocess_sandbox import (
    SubprocessSandbox,
    _probe_interpreter,
    resolve_python,
)


async def test_execute_normal_code(tmp_path) -> None:
    """正常执行：代码打印 + 写产物文件，结果/错误流分离。"""
    sandbox = SubprocessSandbox(timeout=30)
    result = await sandbox.execute(
        code="print('hello')\nwith open('out.txt', 'w') as f:\n    f.write('result')",
        files={},
        work_dir=tmp_path,
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.timed_out is False
    assert "out.txt" in result.artifacts


async def test_execute_timeout_kills(tmp_path) -> None:
    """超时强杀：死循环代码在超时后被强杀，返回 timed_out。"""
    sandbox = SubprocessSandbox(timeout=1)  # 短超时便于测试
    result = await sandbox.execute(
        code="import time\nwhile True:\n    time.sleep(0.1)",
        files={},
        work_dir=tmp_path,
    )
    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.error and "超时" in result.error


async def test_execute_stderr_captured(tmp_path) -> None:
    """错误流分离：代码抛异常时 stderr 捕获，错误结构化返回。"""
    sandbox = SubprocessSandbox(timeout=30)
    result = await sandbox.execute(
        code="raise ValueError('boom')",
        files={},
        work_dir=tmp_path,
    )
    assert result.exit_code != 0
    assert "ValueError" in result.stderr or "boom" in result.stderr


async def test_escape_path_write_rejected(tmp_path) -> None:
    """逃逸防护：越权路径（../escape.txt）写入被拒。"""
    sandbox = SubprocessSandbox(timeout=30)
    result = await sandbox.execute(
        code="",
        files={"../escape.txt": "should not be written"},  # 越权路径
        work_dir=tmp_path,
    )
    assert result.error and "越界" in result.error
    assert not (tmp_path.parent / "escape.txt").exists()  # 未实际写入


def test_validate_artifact_path_basic(tmp_path) -> None:
    """路径校验：工作目录内合法，越界非法。"""
    assert validate_artifact_path(tmp_path, "out.txt") is True
    assert validate_artifact_path(tmp_path, "sub/out.txt") is True
    assert validate_artifact_path(tmp_path, "../escape.txt") is False
    assert validate_artifact_path(tmp_path, "/etc/passwd") is False


async def test_env_isolation_no_key(tmp_path) -> None:
    """环境隔离：沙箱子进程不继承 Key 环境变量。"""
    import os

    os.environ["TEST_FAKE_API_KEY"] = "sk-secret-123"
    sandbox = SubprocessSandbox(timeout=30)
    result = await sandbox.execute(
        code="import os\nprint('KEY' if 'TEST_FAKE_API_KEY' in os.environ else 'NO_KEY')",
        files={},
        work_dir=tmp_path,
    )
    assert "NO_KEY" in result.stdout  # Key 未传入沙箱
    del os.environ["TEST_FAKE_API_KEY"]


# ---- 执行体解析（EN-BOX 红线：不依赖 PATH 上的裸 "python"）----


async def test_probe_accepts_current_interpreter() -> None:
    """探测通过：当前解释器可用于执行。"""
    assert await _probe_interpreter(sys.executable) is True


async def test_probe_rejects_missing_binary() -> None:
    """探测拒绝：不存在的二进制不进入候选（商店 stub 类故障在此被排除）。"""
    assert await _probe_interpreter("erdos-not-a-real-binary-xyz") is False


async def test_default_resolution_prefers_current_interpreter(monkeypatch) -> None:
    """默认解析优先用当前解释器（PATH 上的 python 可能是商店占位 stub）。"""
    monkeypatch.delenv("ERDOS_SANDBOX_PYTHON", raising=False)
    assert await resolve_python() == sys.executable


async def test_explicit_override_never_falls_back_silently(monkeypatch) -> None:
    """显式配置的执行体不可用时如实返回 None，不静默换用其他解释器。"""
    monkeypatch.setenv("ERDOS_SANDBOX_PYTHON", "erdos-not-a-real-binary-xyz")
    assert await resolve_python() is None


async def test_unavailable_interpreter_fails_fast(tmp_path) -> None:
    """执行体缺失 → 结构化错误快速返回（旧实现会挂满 timeout 预算）。"""
    sandbox = SubprocessSandbox(timeout=60, python="erdos-not-a-real-binary-xyz")
    started = time.monotonic()
    result = await sandbox.execute(code="print(1)", files={}, work_dir=tmp_path)
    elapsed = time.monotonic() - started
    assert result.exit_code == -1
    assert result.error and "未找到可用的 Python 执行体" in result.error
    assert result.timed_out is False
    assert elapsed < 10, f"应快速失败，实际耗时 {elapsed:.1f}s"


async def test_execute_with_relative_work_dir(tmp_path, monkeypatch) -> None:
    """相对 work_dir 必须可用：曾把脚本路径二次拼接，代码没跑却返回"成功"形状。"""
    monkeypatch.chdir(tmp_path)
    sandbox = SubprocessSandbox(timeout=30)
    result = await sandbox.execute("print('rel-ok')", {}, Path("rel/work"))
    assert result.exit_code == 0, f"exit={result.exit_code} stderr={result.stderr[:200]}"
    assert "rel-ok" in result.stdout
    assert result.error is None
