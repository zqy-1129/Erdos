"""execute_code 工具（W9 首版三件套）：唯一代码执行入口，danger=high。

模型生成的一切代码只经 Sandbox 端口执行（DEC-026/开发文档 §8.2：
模型生成的指令不得在主进程直接执行）；结果/错误结构化，错误可回注修复（EC-S2）。
"""

from pydantic import BaseModel, Field

from engine.sandbox.base import Sandbox, validate_artifact_path
from engine.tools.base import ToolContext, ToolError, ToolResult

_MAX_LANGUAGE = "python"  # v1 仅 Python（依赖预装在版本化执行环境，开发文档 §8.3）


class ExecuteCodeArgs(BaseModel):
    """execute_code 参数 schema（单源 → Registry tool_payloads / JSON Schema）。"""

    code: str = Field(..., min_length=1, max_length=20000, description="要执行的 Python 代码")
    language: str = Field("python", description="执行语言，当前仅支持 python")


def make_execute_code(sandbox: Sandbox):
    """依赖注入：绑定沙箱实例的 execute_code 执行器（可注入 Subprocess/Docker/Fake）。"""

    async def execute(args: ExecuteCodeArgs, ctx: ToolContext) -> ToolResult:
        if args.language != _MAX_LANGUAGE:
            return ToolResult(
                ok=False,
                error=ToolError(
                    code="TOOL_ARGS_INVALID", message=f"暂不支持语言：{args.language}（仅 python）"
                ),
            )
        result = await sandbox.execute(args.code, {}, ctx.work_dir)
        if result.timed_out:
            return ToolResult(
                ok=False,
                error=ToolError(code="TOOL_FAILED", message="执行超时，已被强制终止"),
                summary=result.stdout.strip(),
            )
        if result.exit_code != 0:
            tail = (result.stderr or result.error or "").strip()
            return ToolResult(
                ok=False,
                error=ToolError(code="TOOL_FAILED", message=f"退出码 {result.exit_code}：{tail}"),
                summary=result.stdout.strip(),
            )
        ref = result.artifacts[0] if result.artifacts else None
        if ref is not None and not validate_artifact_path(ctx.work_dir, ref):
            ref = None  # 越权产物路径不外引（逃逸防护，AT-13）
        return ToolResult(ok=True, summary=result.stdout.strip(), result_ref=ref)

    return execute
