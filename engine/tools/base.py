"""Tool Registry 基础类型（EN-REG W9）。

红线（DEC-026 / 开发文档 §8.2）：
- Registry 即 allowlist：未注册工具一律拒绝并留痕，题面/注入不得提升工具权限；
- code/plot 类工具执行经 Sandbox 端口（danger=high），不在编排主线程裸跑；
- 参数以 pydantic 模型单源校验（同时生成 OpenAI function parameters schema），
  校验失败的结构化错误回注模型（可自修复，EC-T2）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class ToolError(BaseModel):
    """结构化工具错误（模型可读，回注修复；不含敏感明文）。"""

    code: str  # TOOL_UNKNOWN / TOOL_ARGS_INVALID / TOOL_FAILED / NOT_AVAILABLE
    message: str


class ToolResult(BaseModel):
    """一次工具执行结果（Registry dispatch 的统一出口）。"""

    ok: bool
    summary: str = ""  # 进入模型上下文（脱敏 + 截断 ≤2000 字符）
    result_ref: str | None = None  # 产物引用（工作目录相对路径）
    duration_ms: int = 0
    error: ToolError | None = None


class ToolContext(BaseModel):
    """dispatch 上下文：任务/阶段/工作目录（由求解内循环 W11 传入）。"""

    task_id: str
    stage: str
    work_dir: Path


class ToolCallLike(Protocol):
    """工具调用请求的最小端口（兼容 adapters.openai_compat.ToolCall 冻结数据类）。"""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def arguments_json(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具规格：名称 + 描述 + 参数模型（单源）+ 危险级 + 执行器（依赖注入）。

    executor 首参运行时保证为 args_model 实例（Registry 经 model_validate_json 构造），
    静态放宽为 Any 以兼容各工具的具体参数类型（协议逆变限制）。
    """

    name: str
    description: str
    args_model: type[BaseModel]
    danger_level: str  # "low" | "high"（high → 必须经沙箱执行）
    executor: Callable[[Any, ToolContext], Awaitable[ToolResult]]  # 首参运行时为 args_model 实例

    def parameter_schema(self) -> dict:
        """args_model → OpenAI function parameters JSON Schema（不手抄双份）。"""
        return self.args_model.model_json_schema()
