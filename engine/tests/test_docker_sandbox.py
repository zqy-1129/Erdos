"""EN-BOX（W10）Docker 沙箱测试：argv 红线矩阵 / 超时回收 / 输出截断 / 孤儿 GC / 降级红线。

真 Docker 集成用例默认跳过（本机无 Docker 或未设 ERDOS_TEST_DOCKER=1）；
单元用例全部经注入 runner（FakeRunner 捕获 argv）完成，不依赖 Docker 守护进程。
"""

import os
from pathlib import Path

import pytest

from engine.sandbox.base import SandboxUnavailableError, scan_artifacts
from engine.sandbox.docker_sandbox import DockerSandbox, gc_orphans
from engine.sandbox.subprocess_sandbox import (
    SubprocessSandbox,
    detect_docker_available,
    make_sandbox,
)


class FakeRunner:
    """捕获 argv 的运行器替身；可编程返回 (code, stdout, stderr) 或抛 TimeoutError。"""

    def __init__(self, results: list | None = None) -> None:
        self.argvs: list[list[str]] = []
        self._results = list(results or [(0, b"ok", b"")])

    async def __call__(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        self.argvs.append(argv)
        result = self._results.pop(0) if self._results else self._results[-1]
        if isinstance(result, Exception):
            raise result
        return result


def _sandbox(runner: FakeRunner, work_dir: Path) -> DockerSandbox:
    return DockerSandbox(timeout=5, runner=runner)


# ----------------------------------------------------------------------
# argv 红线矩阵（DEC-006）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_argv_isolation_flags(tmp_path: Path) -> None:
    """run 参数红线：无网络/内存/CPU/pids 限额/非 root/只读根/tmpfs/任务目录挂载。"""
    runner = FakeRunner()
    work = tmp_path / "work"
    work.mkdir()
    sandbox = _sandbox(runner, work)
    await sandbox.execute("print('hi')", {"data.csv": "a,b\n1,2"}, work)

    argv = runner.argvs[0]
    assert argv[0] == "docker" and "run" in argv and "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("--cpus") + 1] == "1"
    assert argv[argv.index("--pids-limit") + 1] == "128"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert "--read-only" in argv
    assert argv[argv.index("--tmpfs") + 1].startswith("/tmp:rw")
    mount = argv[argv.index("-v") + 1]
    assert mount.startswith(str(work.resolve())) and mount.endswith(":/work")
    assert argv[argv.index("-w") + 1] == "/work"
    assert argv[-2:] == ["python", "_sandbox_script.py"]
    # files 先落工作目录 + 脚本写入
    assert (work / "data.csv").read_text(encoding="utf-8") == "a,b\n1,2"
    assert (work / "_sandbox_script.py").read_text(encoding="utf-8") == "print('hi')"


@pytest.mark.asyncio
async def test_execute_result_and_artifacts_scan(tmp_path: Path) -> None:
    """stdout/stderr 解码 + 产物扫描（排除脚本自身）。"""

    def side_effect_write(argv: list[str]) -> None:
        (tmp_path / "work" / "figure.png").write_bytes(b"png")  # 模拟容器内产物

    class WritingRunner(FakeRunner):
        async def __call__(self, argv: list[str]) -> tuple[int, bytes, bytes]:
            result = await super().__call__(argv)
            if "run" in argv:
                side_effect_write(argv)
            return result

    writing = WritingRunner([(0, b"slope=2.5\n", b"")])
    sandbox = _sandbox(writing, tmp_path / "work")
    result = await sandbox.execute("pass", {}, tmp_path / "work")
    assert result.exit_code == 0 and result.stdout == "slope=2.5\n"
    assert "figure.png" in result.artifacts and "_sandbox_script.py" not in result.artifacts
    assert result.error is None


@pytest.mark.asyncio
async def test_timeout_kills_container_and_reports(tmp_path: Path) -> None:
    """超时：返回 timed_out 结果 + docker rm -f 兜底回收。"""
    runner = FakeRunner([TimeoutError()])
    sandbox = _sandbox(runner, tmp_path / "work")
    result = await sandbox.execute("pass", {}, tmp_path / "work")
    assert result.timed_out is True and result.exit_code == -1
    cleanup = [a for a in runner.argvs if a[:3] == ["docker", "rm", "-f"]]
    assert len(cleanup) == 1 and cleanup[0][3].startswith("erdos-")


@pytest.mark.asyncio
async def test_nonzero_exit_structured_error(tmp_path: Path) -> None:
    """非零退出 → 结构化 error 携带容器退出码。"""
    runner = FakeRunner([(3, b"partial", b"boom")])
    sandbox = _sandbox(runner, tmp_path / "work")
    result = await sandbox.execute("pass", {}, tmp_path / "work")
    assert result.exit_code == 3 and result.error is not None and "3" in result.error


@pytest.mark.asyncio
async def test_output_truncated_to_limit(tmp_path: Path) -> None:
    """输出截断 1MB（防 print 打爆上下文）。"""
    runner = FakeRunner([(0, b"x" * (2_000_000), b"")])
    sandbox = _sandbox(runner, tmp_path / "work")
    result = await sandbox.execute("pass", {}, tmp_path / "work")
    assert len(result.stdout) <= 1_000_000


@pytest.mark.asyncio
async def test_exception_path_cleans_container(tmp_path: Path) -> None:
    """运行器抛非超时异常 → 容器兜底回收后原异常上抛。"""
    runner = FakeRunner([RuntimeError("daemon gone")])
    sandbox = _sandbox(runner, tmp_path / "work")
    with pytest.raises(RuntimeError, match="daemon gone"):
        await sandbox.execute("pass", {}, tmp_path / "work")
    assert any(a[:3] == ["docker", "rm", "-f"] for a in runner.argvs)


@pytest.mark.asyncio
async def test_gc_orphans_removes_only_erdos_containers(tmp_path: Path) -> None:
    """孤儿 GC：ps -aq --filter name=erdos- → 逐个 rm -f，返回回收数。"""
    runner = FakeRunner([(0, b"abc\ndef\n", b""), (0, b"", b""), (0, b"", b"")])
    removed = await gc_orphans(runner=runner)
    assert removed == 2
    assert runner.argvs[0][1:4] == ["ps", "-aq", "--filter"]
    assert runner.argvs[1][1:3] == ["rm", "-f"] and runner.argvs[1][3] == "abc"
    assert runner.argvs[2][3] == "def"


def test_isolation_mode_attribute() -> None:
    """isolation_mode 显式上报（W14 initialize 消费）。"""
    assert DockerSandbox().isolation_mode == "docker"
    assert SubprocessSandbox().isolation_mode == "subprocess"


@pytest.mark.asyncio
async def test_make_sandbox_require_docker_redline(monkeypatch: pytest.MonkeyPatch) -> None:
    """降级红线：REQUIRE_DOCKER=1 且无 Docker → SandboxUnavailableError（不静默退化）。"""
    monkeypatch.setenv("ERDOS_SANDBOX_REQUIRE_DOCKER", "1")

    async def no_docker() -> bool:
        return False

    monkeypatch.setattr("engine.sandbox.subprocess_sandbox.detect_docker_available", no_docker)
    with pytest.raises(SandboxUnavailableError, match="Docker"):
        await make_sandbox()

    monkeypatch.delenv("ERDOS_SANDBOX_REQUIRE_DOCKER")
    fallback = await make_sandbox()
    assert isinstance(fallback, SubprocessSandbox)


@pytest.mark.asyncio
async def test_scan_artifacts_shared_helper(tmp_path: Path) -> None:
    """scan_artifacts 公共函数：排除脚本自身、子目录相对路径。"""
    (tmp_path / "out.csv").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "fig.png").write_bytes(b"p")
    (tmp_path / "_sandbox_script.py").write_text("pass", encoding="utf-8")
    found = scan_artifacts(tmp_path)
    assert sorted(found) == ["out.csv", str(Path("sub") / "fig.png")]


@pytest.mark.asyncio
async def test_real_docker_integration_gated() -> None:
    """真 Docker 集成（仅 ERDOS_TEST_DOCKER=1 且守护进程可用时执行）。"""
    if os.environ.get("ERDOS_TEST_DOCKER") != "1":
        pytest.skip("ERDOS_TEST_DOCKER=1 未设置（默认跳过真 Docker 用例）")
    if not await detect_docker_available():
        pytest.skip("Docker 守护进程不可用")
    sandbox = DockerSandbox(timeout=60)
    result = await sandbox.execute("print('erdos-ok')", {}, Path("build/docker-it"))
    assert result.exit_code == 0 and "erdos-ok" in result.stdout


def test_floating_image_tag_warns_digest_pinned_silent(capsys) -> None:
    """§10 镜像摘要纪律：浮动 tag 构造时 stderr 诊断；摘要引用安静通过。"""
    DockerSandbox(image="python:3.12-slim", runner=FakeRunner([(0, b"", b"")]))
    captured = capsys.readouterr()
    assert "sha256 摘要" in captured.err

    DockerSandbox(
        image="python:3.12-slim@sha256:" + "a" * 64, runner=FakeRunner([(0, b"", b"")])
    )
    assert "sha256 摘要" not in capsys.readouterr().err
