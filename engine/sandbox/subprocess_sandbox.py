"""subprocess 降级沙箱执行器（SP1-4）：Docker 不可用时的降级实现。

安全约束（对齐 SP1-4 红线）：
- 超时强杀（默认 120s）；
- 环境隔离：不传宿主 Key/敏感环境变量，仅白名单 env；
- 路径白名单：cwd 限定工作目录，产物路径校验（逃逸防护）；
- 结果/错误流分离，错误结构化返回。
"""

import asyncio
import os
from pathlib import Path

from engine.sandbox.base import (
    SANDBOX_SCRIPT_NAME,
    ExecutionResult,
    Sandbox,
    SandboxUnavailableError,
    scan_artifacts,
    validate_artifact_path,
)

# 允许透传给沙箱子进程的环境变量白名单（禁止 Key/敏感变量）
_ENV_WHITELIST = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "PYTHONPATH")


class SubprocessSandbox:
    """subprocess 降级沙箱：python 子进程 + 超时强杀 + 环境隔离 + 路径校验。"""

    isolation_mode = "subprocess"  # 显式上报（DEC-006：降级不得宣称为安全隔离）

    def __init__(self, timeout: float = 120.0, memory_limit_mb: int = 2048) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须 >0")
        self._timeout = timeout
        # subprocess 模式无法精确限制内存（Windows Job Object 需额外实现），
        # 此处记录限制供未来接入，Docker 模式由 --memory 精确限制。
        self._memory_limit_mb = memory_limit_mb

    async def execute(self, code: str, files: dict[str, str], work_dir: Path) -> ExecutionResult:
        """执行代码：落 files → 写脚本 → subprocess 执行 → 捕获结果 → 扫产物。"""
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

        # 4. subprocess 执行（cwd=工作目录，超时强杀）
        try:
            proc = await asyncio.create_subprocess_exec(
                "python", str(script),
                cwd=str(work_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            exit_code = proc.returncode or 0
            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                artifacts=scan_artifacts(work_dir),
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecutionResult(
                exit_code=-1, stdout="", stderr="",
                timed_out=True,
                error=f"执行超时（>{self._timeout}s）被强杀",
            )


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


async def make_sandbox(timeout: float = 120.0) -> Sandbox:
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
    return SubprocessSandbox(timeout=timeout)
