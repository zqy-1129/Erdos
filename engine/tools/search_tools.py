"""search_references 工具（W9 首版三件套，占位）：本地参考检索。

EN-19A（19 赛事内容检索集成）待 ARCH 决策与 SP4 数据契约后实现；当前占位
返回 NOT_AVAILABLE（模型可读，可据此调整策略），不阻塞 execute_code/plot_figure。
"""

from pydantic import BaseModel, Field

from engine.tools.base import ToolContext, ToolError, ToolResult


class SearchReferencesArgs(BaseModel):
    """search_references 参数 schema（接口先定，实现随 EN-19A 接入）。"""

    query: str = Field(..., min_length=1, max_length=200, description="检索意图描述")
    top_k: int = Field(3, ge=1, le=5, description="返回条数上限")


async def search_references(args: SearchReferencesArgs, ctx: ToolContext) -> ToolResult:
    """占位执行器：返回结构化 NOT_AVAILABLE（不抛异常，模型可据此降级）。"""
    return ToolResult(
        ok=False,
        error=ToolError(
            code="NOT_AVAILABLE",
            message=f"本地参考检索尚未接入（EN-19A 待 19 赛事数据契约后启用）；查询已登记：{args.query}",
        ),
    )
