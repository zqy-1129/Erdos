"""沙箱执行器（SP1-4）：Docker 优先 / subprocess 降级，同一 execute 接口。"""

from engine.sandbox.base import ExecutionResult, Sandbox, validate_artifact_path
from engine.sandbox.subprocess_sandbox import (
    SubprocessSandbox,
    detect_docker_available,
    make_sandbox,
)

__all__ = [
    "ExecutionResult",
    "Sandbox",
    "SubprocessSandbox",
    "detect_docker_available",
    "make_sandbox",
    "validate_artifact_path",
]
