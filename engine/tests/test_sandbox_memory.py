"""沙箱内存强制守护（W22 · EN-MEM，PRD §6.1「单次执行 ≤2GB 超限强杀」）。

10-10 审计实况：`memory_limit_mb` 存下来不用（注释自认"Windows Job Object 需额外实现"），
所以这条承诺在无 Docker 的 subprocess 主路径上根本不成立，而文档记 SP1-4 已交付。
本文件把判据从"字段已存"换成"**超限真被拦下、且归因正确**"。

三条反直觉的实测事实决定了实现形状（不是我挑的，是机器给的）：
1. `JOB_OBJECT_LIMIT_PROCESS_MEMORY` 的语义是**拒绝后续分配**（子进程 MemoryError），不是杀进程；
   单次大额分配被秒拒时**峰值根本不涨**（900MB 秒拒、峰值仍 6MB）→ 只靠轮询必然漏归因；
2. 观测通道只能用 `QueryInformationJobObject(JobObjectExtendedLimitInformation)` 读回的
   `PeakProcessMemoryUsed`：按文档回忆拼的 `JobObjectBasicAccountingInformation` 布局在本机
   返回个位数垃圾值——**结构体布局必须实测**，否则观测恒为 0、强杀永远不会触发；
3. 阈值不留余量：曾观测到成功跑完的任务峰值 267MB > 限额 256MB，按 95% 预判会**误杀正常任务**，
   所以触发线是峰值达到限额本身。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from engine.sandbox import subprocess_sandbox as ss
from engine.sandbox.constants import SANDBOX_MEMORY_LIMIT_MB
from engine.sandbox.subprocess_sandbox import SubprocessSandbox, _resolve_memory_limit_mb
from engine.sandbox.win_job import MemoryJob, new_memory_job

WINDOWS = sys.platform == "win32"
LIMIT_MB = 128  # 用例用小限额：既真能触发拒绝，又不让回归吃掉几 GB 内存

SKIP_NO_JOB = pytest.mark.skipif(
    not WINDOWS or not new_memory_job(LIMIT_MB).created,
    reason='Job Object 不可用（非 Windows 或作业创建失败）；不可用时走「如实上报不生效」那条用例',
)

ALLOC_BIG = "buf = bytearray(600 * 1024 * 1024)\nprint('unreachable')\n"


def alloc_gradual(sleep: float) -> str:
    return (
        "import time\nbuf = []\n"
        "for i in range(200):\n"
        "    buf.append(bytearray(4 * 1024 * 1024))\n"
        f"    time.sleep({sleep})\n"
        "print('done')\n"
    )


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tasks" / "t1"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def box() -> SubprocessSandbox:
    return SubprocessSandbox(timeout=30, memory_limit_mb=LIMIT_MB)


# ----------------------------------------------------------------------
# 超限拦得下、归因归得对（真机执行）
# ----------------------------------------------------------------------
@SKIP_NO_JOB
@pytest.mark.parametrize("code", [ALLOC_BIG, alloc_gradual(0), alloc_gradual(0.02)],
                         ids=["单笔大额", "渐进快", "渐进慢"])
def test_over_limit_execution_is_stopped_and_attributed(
    box: SubprocessSandbox, work_dir: Path, code: str
) -> None:
    """三种分配形态：没跑成功 + 归因到内存 + 文案给出出路（模型要靠它自修复）。"""
    result = asyncio.run(box.execute(code, {}, work_dir))
    assert result.memory_exceeded is True, (result.exit_code, result.stderr[-200:])
    assert "unreachable" not in result.stdout and "done" not in result.stdout
    assert f"{LIMIT_MB}MB" in (result.error or "")
    assert "PRD §6.1" in (result.error or ""), result.error


@SKIP_NO_JOB
def test_within_limit_execution_is_not_flagged(box: SubprocessSandbox, work_dir: Path) -> None:
    """正向对照（防"守护恒真"）：小内存任务必须正常通过且不被标成超限。"""
    result = asyncio.run(box.execute("print(sum(range(1000)))\n", {}, work_dir))
    assert result.exit_code == 0 and result.memory_exceeded is False
    assert "499500" in result.stdout  # sum(range(1000))


@SKIP_NO_JOB
def test_ordinary_crash_is_not_misattributed(box: SubprocessSandbox, work_dir: Path) -> None:
    """ValueError 崩溃不能判成内存超限：归因写错，模型会拿着错结论去修。"""
    result = asyncio.run(box.execute("raise ValueError('boom')\n", {}, work_dir))
    assert result.exit_code != 0 and result.memory_exceeded is False


@SKIP_NO_JOB
def test_capability_is_reported_truthfully(box: SubprocessSandbox) -> None:
    assert box.memory_enforced is True
    assert box.memory_enforcement_reason == ""


# ----------------------------------------------------------------------
# 能力不可用时：如实上报，不静默放宽（DEC-006）
# ----------------------------------------------------------------------
def test_unavailable_capability_is_reported_with_reason_and_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notes: list[str] = []

    class _DeadJob:
        """不跑 __post_init__ 的桩：真机作业会被 post_init 又建起来，created 会被改回 True。"""

        def __init__(self, limit_mb: int) -> None:
            self.limit_bytes = limit_mb * 1024 * 1024
            self.created = False
            self.reason = "作业创建失败(x)"
            self.assigned_pids: list[int] = []

        def assign(self, pid: int) -> bool:
            return False

        def peak_memory_bytes(self) -> int | None:
            return None

        def terminate(self) -> bool:
            return False

        def close(self) -> None:
            pass

    def _dead_job(limit_mb: int) -> _DeadJob:
        return _DeadJob(limit_mb)

    monkeypatch.setattr(ss, "new_memory_job", _dead_job)
    monkeypatch.setattr(sys.stderr, "write", lambda s: notes.append(s))
    sandbox = SubprocessSandbox(timeout=10, memory_limit_mb=64)

    assert sandbox.memory_enforced is False, "作业建不起来却报告已强制 = 谎报"
    assert "作业创建失败(x)" in sandbox.memory_enforcement_reason
    asyncio.run(sandbox.execute("print(1)\n", {}, tmp_path))
    note = "".join(notes)
    assert "内存限额未生效" in note and "isolation_mode=subprocess" in note, note


def test_observation_failure_does_not_claim_enforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, box: SubprocessSandbox
) -> None:
    """峰值读不到时不能继续装作在管：停止判定并如实说明（观测恒 0 = 强杀永不触发）。"""
    notes: list[str] = []

    class _BlindJob:
        created = True
        limit_bytes = LIMIT_MB * 1024 * 1024
        reason = ""

        def assign(self, pid: int) -> bool:
            return True

        def peak_memory_bytes(self) -> None:
            return None

        def terminate(self) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr(ss, "new_memory_job", lambda mb: _BlindJob())
    monkeypatch.setattr(ss, "_MEMORY_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(sys.stderr, "write", lambda s: notes.append(s))
    asyncio.run(box.execute("print(1)\n", {}, tmp_path))
    assert any("观测" in n for n in notes), notes


# ----------------------------------------------------------------------
# 归因优先级（单元级：三信号的先后顺序，不依赖真机内存行为）
# ----------------------------------------------------------------------
def _job_stub(limit_mb: int = LIMIT_MB) -> MemoryJob:
    job = MemoryJob(limit_bytes=limit_mb * 1024 * 1024)
    job.created = False  # 单元断言里不需要真句柄
    return job


def test_engine_termination_takes_precedence_in_wording(tmp_path: Path) -> None:
    box = SubprocessSandbox(timeout=5, memory_limit_mb=LIMIT_MB)
    job = _job_stub()
    message = box._attribute_memory(job, {"exceeded": True}, exit_code=1, peak=None, stderr_text="")
    assert message and "已由引擎终止" in message, message


def test_peak_over_limit_is_attributed_even_without_memoryerror(tmp_path: Path) -> None:
    box = SubprocessSandbox(timeout=5, memory_limit_mb=LIMIT_MB)
    job = _job_stub()
    message = box._attribute_memory(
        job, {"exceeded": False}, exit_code=1, peak=job.limit_bytes, stderr_text="some other crash"
    )
    assert message and "逼近沙箱限额" in message, message


def test_memoryerror_signal_covers_the_no_peak_case(tmp_path: Path) -> None:
    """单笔大额秒拒时峰值不涨：只有 MemoryError 信号能归因（实测 900MB→峰值 6MB）。"""
    box = SubprocessSandbox(timeout=5, memory_limit_mb=LIMIT_MB)
    job = _job_stub()
    message = box._attribute_memory(
        job, {"exceeded": False}, exit_code=1, peak=6 * 1024 * 1024, stderr_text="MemoryError"
    )
    assert message and "被沙箱限额拒绝" in message, message


def test_successful_run_is_never_attributed_to_memory(tmp_path: Path) -> None:
    box = SubprocessSandbox(timeout=5, memory_limit_mb=LIMIT_MB)
    job = _job_stub()
    assert box._attribute_memory(job, {"exceeded": True}, exit_code=0, peak=99 * 1024**2,
                                 stderr_text="MemoryError") is None


# ----------------------------------------------------------------------
# 零回归：超时、env、Key 白名单
# ----------------------------------------------------------------------
def test_timeout_path_unchanged(tmp_path: Path) -> None:
    """加了作业对象后超时路径必须原样（进程树清理仍由 KILL_ON_JOB_CLOSE 兜底）。"""
    sandbox = SubprocessSandbox(timeout=2, memory_limit_mb=LIMIT_MB)
    result = asyncio.run(sandbox.execute("import time\ntime.sleep(30)\n", {}, tmp_path))
    assert result.timed_out is True and result.memory_exceeded is False
    assert "超时" in (result.error or "")


def test_job_handle_closed_even_on_timeout_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """异常/超时路径也要关句柄：不关就是留着孤儿 python 进程（本机 TaskStop 踩过同款坑）。"""
    closed: list[bool] = []
    real_new = ss.new_memory_job

    def tracked(limit_mb: int) -> MemoryJob:
        job = real_new(limit_mb)
        original = job.close
        def close_and_track() -> None:
            closed.append(True)
            original()

        job.close = close_and_track  # type: ignore[method-assign]
        return job

    monkeypatch.setattr(ss, "new_memory_job", tracked)
    sandbox = SubprocessSandbox(timeout=2, memory_limit_mb=LIMIT_MB)
    asyncio.run(sandbox.execute("import time\ntime.sleep(30)\n", {}, tmp_path))
    assert closed, "超时路径没关作业句柄"


def test_env_override_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法值不静默回落：回落会让"我设了 4096"看起来生效而实际没生效。"""
    monkeypatch.delenv("ERDOS_SANDBOX_MEMORY_MB", raising=False)
    assert _resolve_memory_limit_mb(SANDBOX_MEMORY_LIMIT_MB) == SANDBOX_MEMORY_LIMIT_MB
    monkeypatch.setenv("ERDOS_SANDBOX_MEMORY_MB", "1024")
    assert _resolve_memory_limit_mb(SANDBOX_MEMORY_LIMIT_MB) == 1024
    for bad in ("abc", "0", "-1", " "):
        monkeypatch.setenv("ERDOS_SANDBOX_MEMORY_MB", bad)
        with pytest.raises(ValueError):
            _resolve_memory_limit_mb(SANDBOX_MEMORY_LIMIT_MB)


@SKIP_NO_JOB
def test_key_env_still_blocked_inside_sandbox(work_dir: Path) -> None:
    """内存改造不得动到环境变量白名单红线（Key 不进沙箱）。"""
    sandbox = SubprocessSandbox(timeout=10, memory_limit_mb=LIMIT_MB)
    code = "import os\nprint(os.environ.get('ERDOS_MODEL_API_KEY', 'ABSENT'))\n"
    result = asyncio.run(sandbox.execute(code, {}, work_dir))
    assert "ABSENT" in result.stdout, result.stdout


def test_observation_channel_reads_real_bytes_on_windows(tmp_path: Path) -> None:
    """结构体偏移的守门用例：读数必须像"内存字节数"，个位数说明又猜错了布局。"""
    if not WINDOWS:
        pytest.skip("Job Object 是 Windows 机制")
    job = new_memory_job(LIMIT_MB)
    assert job.created, job.reason
    script = tmp_path / "p.py"
    script.write_text(alloc_gradual(0.02), encoding="utf-8")
    seen: list[int] = []
    try:
        async def run() -> None:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), cwd=str(tmp_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            assert job.assign(proc.pid)
            while proc.returncode is None:
                peak = job.peak_memory_bytes()
                if peak is not None:
                    seen.append(peak)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.05)
                except TimeoutError:
                    pass
            await proc.communicate()

        asyncio.run(asyncio.wait_for(run(), timeout=30))
    finally:
        job.close()
    assert seen, "全程读不到峰值"
    assert max(seen) > 1_000_000, f"读数不像内存字节数（可能是结构体偏移错了）：{max(seen)}"
