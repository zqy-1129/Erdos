"""subprocess 降级沙箱执行器（SP1-4）：Docker 不可用时的降级实现。

安全约束（对齐 SP1-4 红线）：
- 超时强杀（默认 120s）；
- 环境隔离：不传宿主 Key/敏感环境变量，仅白名单 env；
- 路径白名单：cwd 限定工作目录，产物路径校验（逃逸防护）；
- 结果/错误流分离，错误结构化返回。
"""

import asyncio
import contextlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from engine.sandbox.base import (
    SANDBOX_SCRIPT_NAME,
    ExecutionResult,
    Sandbox,
    SandboxUnavailableError,
    scan_artifacts,
    validate_artifact_path,
)
from engine.sandbox.constants import SANDBOX_MEMORY_LIMIT_MB, SANDBOX_TIMEOUT_SECONDS
from engine.sandbox.win_job import TRIGGER_RATIO, MemoryJob, new_memory_job

# 允许透传给沙箱子进程的环境变量白名单（禁止 Key/敏感变量）
_ENV_WHITELIST = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "PYTHONPATH")

# 解释器探测超时：探测失败要快，不能占用求解阶段的 120s 执行预算
_INTERPRETER_PROBE_TIMEOUT = 5.0
# 内存峰值轮询间隔：够密以致"先于子进程自身 MemoryError"拿到归因，够疏以不吃掉 CPU
_MEMORY_POLL_INTERVAL = 0.05

# 突变大额分配被作业拒绝时"峰值不涨、进程秒退"（本机实测），只能靠 MemoryError 信号归因
_MEMORY_ERROR_RE = re.compile(r"\bMemoryError\b")

MEMORY_LIMIT_ENV = "ERDOS_SANDBOX_MEMORY_MB"


def _resolve_memory_limit_mb(default_mb: int) -> int:
    """`ERDOS_SANDBOX_MEMORY_MB` 覆盖默认限额（大内存题的排障口子）。

    非法值**不静默回落到默认**：回落会让"我设了 4096"在日志里看起来生效而实际没生效。
    构造期抛 ValueError → 由入口转诊断码 2（与 ERDOS_TOOL_MODE 同一口径）。
    """
    raw = os.environ.get(MEMORY_LIMIT_ENV)
    if not raw:
        return default_mb
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(
            f"{MEMORY_LIMIT_ENV}={raw!r} 非法：需整数 MB（例如 2048）"
        ) from exc
    if value <= 0:
        raise ValueError(f"{MEMORY_LIMIT_ENV}={value} 非法：必须 >0")
    return value


async def _probe_interpreter(python: str, code: str = "pass") -> bool:
    """候选解释器能否真正执行代码（商店占位 stub / 不存在路径都会在这里被排除）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            python, "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return False
    try:
        _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=_INTERPRETER_PROBE_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    return proc.returncode == 0


async def resolve_python(explicit: str | None = None) -> str | None:
    """解析沙箱可用的 Python 执行体，找不到返回 None（不静默退化为"裸跑"）。

    优先级：`ERDOS_SANDBOX_PYTHON`（打包分发时由壳指向随附解释器）→ 未冻结时的
    `sys.executable`（开发/测试环境，确定性最好）→ PATH 上的 python3/python（逐个探测）。
    为什么不能写死 `"python"`：Windows 上它常解析到微软商店的 App Execution Alias 占位
    stub，GUI 父进程派生时会挂到执行超时上限；PyInstaller 冻结后 `sys.executable` 又是
    引擎自身 exe，必须由 `ERDOS_SANDBOX_PYTHON` 指定随附解释器（正式隔离路径是 Docker）。
    """
    override = explicit or os.environ.get("ERDOS_SANDBOX_PYTHON")
    path_candidates = [p for p in (shutil.which("python3"), shutil.which("python")) if p]
    if override:
        # 显式配置就只认它：探测失败如实报错，不静默回退到别的解释器（EC 原则 2）
        candidates: list[str] = [override]
    elif getattr(sys, "frozen", False):
        # PyInstaller 冻结后 sys.executable 是引擎自身 exe，只能走随附解释器或 PATH 探测
        candidates = path_candidates
    else:
        candidates = [sys.executable, *path_candidates]
    workable: list[str] = []
    for cand in candidates:
        if not await _probe_interpreter(cand):
            continue
        # 能跑 pass ≠ 能跑建模代码：PATH 上的解释器可能缺 numpy/matplotlib，让模型代码
        # 以 ModuleNotFoundError 形式伪装成工具失败。优先取带建模依赖的那个。
        if await _probe_interpreter(cand, "import numpy"):
            return cand
        workable.append(cand)
    return workable[0] if workable else None


class SubprocessSandbox:
    """subprocess 降级沙箱：python 子进程 + 超时强杀 + 内存限额 + 环境隔离 + 路径校验。"""

    isolation_mode = "subprocess"  # 显式上报（DEC-006：降级不得宣称为安全隔离）

    def __init__(
        self,
        timeout: float = SANDBOX_TIMEOUT_SECONDS,
        memory_limit_mb: int = SANDBOX_MEMORY_LIMIT_MB,
        python: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须 >0")
        self._timeout = timeout
        self._memory_limit_mb = _resolve_memory_limit_mb(memory_limit_mb)
        self._python = python  # 显式指定执行体（打包分发时由壳传入随附解释器路径）
        self._resolved: str | None | bool = False  # False=未探测；None=探测后确认不可用
        self._memory_probe: MemoryJob | None = None  # 首次执行时建一次，用于如实上报能力
        self._memory_note_shown = False  # "限额未生效"只说一次，不淹stderr

    # ------------------------------------------------------------------
    @property
    def memory_enforced(self) -> bool:
        """内存限额是否**真的生效**（不是"字段已存"）。

        判据是作业对象能否建起来并绑定成功，而不是 `memory_limit_mb` 是否 >0——后者正是
        10-10 审计抓到的形状：限额存下来不用，文档却记 PRD §6.1 已满足。
        首次执行前用一次探测（不绑进程）确定能力，之后复用结论。
        """
        if self._memory_probe is None:
            self._memory_probe = new_memory_job(self._memory_limit_mb)
        return self._memory_probe.created

    @property
    def memory_enforcement_reason(self) -> str:
        """不生效时的原因（DEC-006：不得静默放宽，必须可解释）。"""
        if self.memory_enforced:
            return ""
        return (self._memory_probe.reason if self._memory_probe else "尚未探测") or "未知原因"

    async def execute(self, code: str, files: dict[str, str], work_dir: Path) -> ExecutionResult:
        """执行代码：落 files → 写脚本 → subprocess 执行 → 捕获结果 → 扫产物。"""
        # 绝对化：子进程 cwd=work_dir 后，相对脚本路径会被二次拼接（"can't open file"，
        # 退出码 2 但代码从未执行），求解阶段会拿着空输出继续走。
        work_dir = work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        # 1. 落 files 到工作目录（路径校验，防逃逸）
        for rel_path, content in files.items():
            if not validate_artifact_path(work_dir, rel_path):
                return ExecutionResult(
                    exit_code=1, stdout="", stderr="",
                    error=f"文件路径越界被拒：{rel_path}",
                )
            target = work_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        # 2. 写 code 到临时脚本（工作目录内）
        script = work_dir / SANDBOX_SCRIPT_NAME
        script.write_text(code, encoding="utf-8")

        # 3. 受限环境变量（不传 Key/敏感变量）
        env = {k: v for k, v in os.environ.items() if k in _ENV_WHITELIST}

        # 4. 解析执行体（探测结果缓存复用；不可用则快速失败，不烧掉执行超时预算）
        if self._resolved is False:
            self._resolved = await resolve_python(self._python)
        python = self._resolved if isinstance(self._resolved, str) else None
        if python is None:
            return ExecutionResult(
                exit_code=-1, stdout="", stderr="",
                error=(
                    "未找到可用的 Python 执行体（subprocess 降级模式需随附解释器）："
                    "请设置 ERDOS_SANDBOX_PYTHON 指向可用 python，或启用 Docker 沙箱"
                ),
            )

        # 5. subprocess 执行（cwd=工作目录，超时强杀 + 内存限额观测）
        #    作业对象每次执行新建：一次作业的峰值累计会污染下一次的归因。
        job = new_memory_job(self._memory_limit_mb)
        verdict = {"exceeded": False, "killed": False}
        try:
            proc = await asyncio.create_subprocess_exec(
                python, str(script),
                cwd=str(work_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            bound = job.assign(proc.pid) if job.created else False
            if not job.created:
                # 作业根本建不起来：限额这条承诺没兑现，必须说一次（不静默放宽，DEC-006）
                self._note_memory_inactive(job.reason or "作业创建失败")
            elif not bound:
                self._note_memory_inactive(f"作业已建但绑定子进程失败：{job.reason}")
            watcher = (
                asyncio.create_task(self._watch_memory(job, proc, verdict))
                if job.created and bound
                else None
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    exit_code=-1, stdout="", stderr="",
                    timed_out=True,
                    error=f"执行超时（>{self._timeout}s）被强杀",
                )
            finally:
                if watcher is not None:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher

            exit_code = proc.returncode or 0
            stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
            # 峰值必须在关句柄前读：close 之后作业已销毁
            peak = job.peak_memory_bytes() if job.created else None
            memory_error = self._attribute_memory(job, verdict, exit_code, peak, stderr_text)
            if memory_error is not None:
                return ExecutionResult(
                    exit_code=exit_code,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    memory_exceeded=True,
                    error=memory_error,
                )
            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                artifacts=scan_artifacts(work_dir),
            )
        finally:
            # KILL_ON_JOB_CLOSE：异常/取消路径关句柄即兜底杀掉整棵进程树
            job.close()

    def _attribute_memory(
        self, job: MemoryJob, verdict: dict[str, bool], exit_code: int, peak: int | None, stderr_text: str
    ) -> str | None:
        """超限归因：返回模型可读文案，非内存问题返回 None。

        三个信号必须组合用——这是本机实测出来的，不是设计偏好：
        - **引擎主动终止**（轮询到峰值越线）：只有"缓慢涨上去"的任务抢得到；
        - **观测峰值逼近限额**：同样只覆盖渐进增长；
        - **子进程 MemoryError**：突变大额分配被作业直接拒绝时**峰值不涨**（实测 900MB 秒拒、
          峰值仍是 6MB，0.06s 就退出），此时前两个信号都是空的。
        只靠轮询做归因，会把最常见的"一次性要太多"判成普通代码崩溃——归因写错，模型拿着
        错的结论去自修复，求解内循环就修不到点上。
        """
        if exit_code == 0:
            return None
        limit_mb = self._memory_limit_mb
        if verdict.get("exceeded"):
            return (
                f"内存使用超过沙箱限额 {limit_mb}MB，已由引擎终止"
                "（PRD §6.1 单次执行 ≤2GB）；请降低峰值内存或改分批计算"
            )
        if peak is not None and peak >= job.limit_bytes * TRIGGER_RATIO:
            return (
                f"内存占用逼近沙箱限额 {limit_mb}MB（观测峰值 {int(peak / 1024 / 1024)}MB）"
                "且进程未正常退出；请降低峰值内存"
            )
        if _MEMORY_ERROR_RE.search(stderr_text):
            return (
                f"分配内存被沙箱限额拒绝（{limit_mb}MB，PRD §6.1 ≤2GB）：子进程 MemoryError；"
                "请降低单次分配规模或分批计算"
            )
        return None

    async def _watch_memory(self, job: MemoryJob, proc: Any, verdict: dict[str, bool]) -> None:
        """轮询作业峰值内存；越过阈值由引擎终止子进程并打标（观测不可用则不动作）。"""
        threshold = int(job.limit_bytes * TRIGGER_RATIO)
        while True:
            await asyncio.sleep(_MEMORY_POLL_INTERVAL)
            peak = job.peak_memory_bytes()
            if peak is None:
                # 观测失败不能当成"没用内存"：停止判定，但绝不谎称已强制（DEC-006）
                self._note_memory_inactive("峰值内存观测失败，本次执行的内存限额不可保证")
                return
            if peak >= threshold:
                verdict["exceeded"] = True
                verdict["killed"] = job.terminate() or proc.returncode is None
                return

    def _note_memory_inactive(self, reason: str) -> None:
        """限额未生效必须可见（一次性，不淹 stderr，也不静默放宽）。"""
        if self._memory_note_shown:
            return
        self._memory_note_shown = True
        sys.stderr.write(
            f"[engine] 沙箱内存限额未生效（{self._memory_limit_mb}MB 期望）：{reason}"
            f"｜isolation_mode={self.isolation_mode}（不宣称等价隔离，DEC-006）\n"
        )
        sys.stderr.flush()


async def detect_docker_available() -> bool:
    """探测 Docker 是否可用（可执行 + 守护进程可达）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "version", "--format", "{{.Server.Version}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        return proc.returncode == 0 and bool(stdout.strip())
    except (TimeoutError, FileNotFoundError):
        return False


async def make_sandbox(
    timeout: float = SANDBOX_TIMEOUT_SECONDS, python: str | None = None
) -> Sandbox:
    """沙箱工厂（EN-BOX W10）：Docker 优先，不可用降级 subprocess（EC-S3）。

    降级红线（DEC-006）：`ERDOS_SANDBOX_REQUIRE_DOCKER=1` 时环境无 Docker 直接抛
    SandboxUnavailableError（正式构建引导配置 Docker），不静默退化为不安全执行。
    返回实例带 isolation_mode 属性（"docker" | "subprocess"），由 W14 initialize 上报。
    """
    from engine.sandbox.docker_sandbox import DockerSandbox  # 局部导入避免环

    if await detect_docker_available():
        return DockerSandbox(timeout=timeout)
    if os.environ.get("ERDOS_SANDBOX_REQUIRE_DOCKER") == "1":
        raise SandboxUnavailableError(
            "要求 Docker 沙箱但环境不可用（ERDOS_SANDBOX_REQUIRE_DOCKER=1）；"
            "请安装并启动 Docker Desktop 后重试"
        )
    return SubprocessSandbox(timeout=timeout, python=python)
