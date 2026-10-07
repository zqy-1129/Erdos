"""Docker 沙箱执行器（EN-BOX W10）：默认模式，满足 DEC-006 隔离要求。

红线（开发文档 §8.3 / DEC-006）：
- 镜像经 ERDOS_SANDBOX_IMAGE 锁定（生产必须是 sha256 摘要引用，禁浮 latest）；
- --network=none（求解阶段运行时不联网）；非 root 用户（65534）+ 只读根文件系统
  （tmpfs /tmp 供临时文件）；资源限额 memory/cpus/pids 显式配置（Docker 默认无上限）；
- 工作目录以 bind mount 只挂载任务子目录（宿主其余不可见），输出截断 1MB；
- 容器一次性（run --rm），超时/异常 docker rm -f 兜底 + gc_orphans 孤儿回收。
Windows 容器语义差异（如路径挂载、用户映射）在 docs/ 清单登记（W10 验收项）。
"""

import asyncio
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from engine.sandbox.base import (
    SANDBOX_SCRIPT_NAME,
    ExecutionResult,
    scan_artifacts,
)

OUTPUT_LIMIT = 1_000_000  # 输出截断 1MB（防 print 打爆上下文）
DEFAULT_IMAGE = "python:3.12-slim"
SANDBOX_USER = "65534:65534"  # nobody：非 root 运行
CONTAINER_PREFIX = "erdos-"


class DockerSandbox:
    """Docker 沙箱：docker run 一次性容器 + 显式资源限额 + 无网络。"""

    isolation_mode = "docker"

    def __init__(
        self,
        timeout: float = 120.0,
        memory: str = "2g",
        cpus: str = "1",
        pids_limit: int = 128,
        image: str | None = None,
        docker_bin: str = "docker",
        runner: Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]] | None = None,
    ) -> None:
        self._timeout = timeout
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._image = image or os.environ.get("ERDOS_SANDBOX_IMAGE", DEFAULT_IMAGE)
        if "@" not in self._image:
            # §10：生产镜像必须是 sha256 摘要引用（禁浮 tag）。开发环境允许浮 tag
            # 便利，但诊断面必须可见；打包产物（SP5-3/W17）构建期固化摘要。
            sys.stderr.write(
                f"[engine] 警告：沙箱镜像未含 sha256 摘要（{self._image}）；"
                "开发可接受，生产须经 ERDOS_SANDBOX_IMAGE 固定摘要\n"
            )
        self._docker_bin = docker_bin
        # 运行器注入点：测试替身 / 真实 asyncio subprocess
        self._run = runner or self._run_process

    async def _run_process(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        """真实执行器：create_subprocess_exec + 总时长兜底超时。"""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout + 10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, stdout_b, stderr_b

    async def execute(self, code: str, files: dict[str, str], work_dir: Path) -> ExecutionResult:
        """执行代码：files 先落工作目录（与 SubprocessSandbox 同语义）。"""
        work_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            target = work_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        script = work_dir / SANDBOX_SCRIPT_NAME
        script.write_text(code, encoding="utf-8")

        container = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:12]}"
        argv = [
            self._docker_bin, "run", "--rm",
            "--name", container,
            "--network", "none",                      # 无网络（求解运行时红线）
            "--memory", self._memory,                 # 资源限额显式配置
            "--cpus", self._cpus,
            "--pids-limit", str(self._pids_limit),
            "--user", SANDBOX_USER,                   # 非 root
            "--read-only",                            # 只读根文件系统
            "--tmpfs", "/tmp:rw,size=64m,noexec",
            "-v", f"{work_dir.resolve()}:/work",      # 只挂载任务子目录
            "-w", "/work",
            self._image,
            "python", SANDBOX_SCRIPT_NAME,
        ]
        try:
            returncode, stdout_b, stderr_b = await self._run(argv)
        except TimeoutError:
            await self._force_remove(container)
            return ExecutionResult(
                exit_code=-1, stdout="", stderr="",
                timed_out=True,
                error=f"执行超时（>{self._timeout}s），容器已强杀回收",
            )
        except BaseException:
            await self._force_remove(container)  # 异常路径容器兜底回收
            raise
        return ExecutionResult(
            exit_code=returncode,
            stdout=stdout_b.decode("utf-8", errors="replace")[:OUTPUT_LIMIT],
            stderr=stderr_b.decode("utf-8", errors="replace")[:OUTPUT_LIMIT],
            error=None if returncode == 0 else f"容器退出码 {returncode}",
            artifacts=scan_artifacts(work_dir),
        )

    async def _force_remove(self, container: str) -> None:
        """docker rm -f：超时/异常路径的容器兜底回收（尽力而为，失败不掩盖原异常）。"""
        try:
            await self._run([self._docker_bin, "rm", "-f", container])
        except Exception:  # noqa: BLE001 - 兜底回收失败不改变主流程结果
            pass


async def gc_orphans(docker_bin: str = "docker", runner: Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]] | None = None) -> int:
    """孤儿容器 GC：回收所有 erdos-* 容器（启动恢复流程调用，开发文档 §5.5）。"""
    run = runner or (lambda argv: DockerSandbox(docker_bin=docker_bin)._run_process(argv))
    code, stdout_b, _ = await run(
        [docker_bin, "ps", "-aq", "--filter", f"name={CONTAINER_PREFIX}"]
    )
    if code != 0:
        return 0
    ids = [x for x in stdout_b.decode("utf-8", errors="replace").split() if x]
    removed = 0
    for cid in ids:
        rc, _, _ = await run([docker_bin, "rm", "-f", cid])
        removed += 1 if rc == 0 else 0
    return removed
