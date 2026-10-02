"""沙箱执行器单元测试（SP1-4）：正常执行 / 超时强杀 / 逃逸防护 / 环境隔离。"""



from engine.sandbox.base import validate_artifact_path
from engine.sandbox.subprocess_sandbox import SubprocessSandbox


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
