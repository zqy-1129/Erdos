"""Erdos 引擎工具层（EN-REG W9）：可插拔 Tool Registry + 首版三件套。

职责边界（DEC-026）：工具的注册、allowlist、参数校验与执行全部在本包内完成；
事件发射（tool.call/tool.result）与留痕（record_tool_call）经构造注入，
不反向依赖编排器与 IPC 方法层。
"""

from engine.tools.base import ToolCallLike, ToolContext, ToolError, ToolResult, ToolSpec
from engine.tools.code_tools import ExecuteCodeArgs, make_execute_code
from engine.tools.plot_tools import PlotFigureArgs, SeriesSpec, build_plot_script, make_plot_figure
from engine.tools.registry import ToolRegistry, clip, mask_secrets
from engine.tools.search_tools import SearchReferencesArgs, search_references


def build_default_registry(
    sandbox,
    events=None,  # noqa: ANN001 - EventEmitter | None
    trail=None,  # noqa: ANN001 - TrailRecorder | None
) -> ToolRegistry:
    """首版工具三件套：execute_code / plot_figure / search_references（占位）。

    新增工具必须走 ARCH 评审后在此登记（主计划 §6.3 风险 7：防工具面蔓延）。
    """
    registry = ToolRegistry(events=events, trail=trail)
    registry.register(
        ToolSpec(
            name="execute_code",
            description="在隔离沙箱执行 Python 代码，返回 stdout 与结构化错误（唯一代码执行入口）",
            args_model=ExecuteCodeArgs,
            danger_level="high",
            executor=make_execute_code(sandbox),
        )
    )
    registry.register(
        ToolSpec(
            name="plot_figure",
            description="按图表规格生成 matplotlib 绘图脚本并在沙箱执行，产出 figure.png",
            args_model=PlotFigureArgs,
            danger_level="high",
            executor=make_plot_figure(sandbox),
        )
    )
    registry.register(
        ToolSpec(
            name="search_references",
            description="本地历史题目/优秀论文参考检索（EN-19A 接入前返回 NOT_AVAILABLE）",
            args_model=SearchReferencesArgs,
            danger_level="low",
            executor=search_references,
        )
    )
    return registry


__all__ = [
    "ExecuteCodeArgs",
    "PlotFigureArgs",
    "SearchReferencesArgs",
    "SeriesSpec",
    "ToolCallLike",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_default_registry",
    "build_plot_script",
    "clip",
    "mask_secrets",
    "search_references",
]
