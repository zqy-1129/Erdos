"""沙箱执行器接口（SP1-4）：双模式（Docker 优先 / subprocess 降级），同一 execute 接口。

红线（SP1-4 提示词）：
- 沙箱内进程禁止访问 Key 环境变量或宿主网络；
- 产物只写任务工作目录，越权路径写入被拒；
- 结果与错误流分离返回，错误结构化（模型可读）。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class ExecutionResult:
    """一次代码执行结果（结果/错误流分离）。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None  # 结构化错误（模型可读，供求解内循环回传）
    artifacts: list[str] = field(default_factory=list)  # 产物相对路径（工作目录内）


class Sandbox(Protocol):
    """沙箱执行器端口：execute(code, files, work_dir) -> ExecutionResult。"""

    async def execute(self, code: str, files: dict[str, str], work_dir: Path) -> ExecutionResult:
        """执行代码；files 为 {相对路径: 内容}，先落工作目录，产物只写工作目录。"""
        ...


def validate_artifact_path(work_dir: Path, rel_path: str) -> bool:
    """校验产物相对路径是否在工作目录内（逃逸防护）。

    - 绝对路径 / 含 `..` 越界 / 符号链接逃逸 → False；
    - 规范化后仍位于 work_dir 内 → True。
    """
    work = work_dir.resolve()
    candidate = (work / rel_path).resolve()
    try:
        candidate.relative_to(work)
        return True
    except ValueError:
        return False
