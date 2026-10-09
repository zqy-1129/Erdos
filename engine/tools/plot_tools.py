"""plot_figure 工具（W9 首版三件套）：图表规格 → 生成绘图脚本 → 沙箱执行。

matplotlib 中文字体按 PRD 工具链清单显式配置（SimHei/雅黑/Noto/文泉驿 回退链，
axes.unicode_minus=False），避免中文标签变豆腐块；产物 figure.png 落工作目录，
经 artifact.ready 事件与 artifact_index 留痕（由调用方接线）。
"""

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from engine.sandbox.base import Sandbox, validate_artifact_path
from engine.tools.base import ToolContext, ToolError, ToolResult

_FIGURE_NAME = "figure.png"


class SeriesSpec(BaseModel):
    """一条数据序列（图例标签 + 数值）。"""

    label: str = Field(..., min_length=1, max_length=100)
    values: list[float] = Field(..., min_length=1, max_length=10000)


class PlotFigureArgs(BaseModel):
    """plot_figure 参数 schema（单源）。"""

    title: str = Field(..., min_length=1, max_length=200)
    x_label: str = Field("", max_length=100)
    y_label: str = Field("", max_length=100)
    kind: Literal["line", "bar", "scatter"] = "line"
    series: list[SeriesSpec] = Field(..., min_length=1, max_length=10)

    @field_validator("series")
    @classmethod
    def series_labels_unique(cls, value: list[SeriesSpec]) -> list[SeriesSpec]:
        labels = [s.label for s in value]
        if len(labels) != len(set(labels)):
            raise ValueError("图例标签重复")
        return value


def build_plot_script(args: PlotFigureArgs) -> str:
    """由参数生成确定性绘图脚本（纯函数，可独立单测）。"""
    series_json = json.dumps([s.model_dump() for s in args.series], ensure_ascii=False)
    return f'''# -*- coding: utf-8 -*-
"""由 Erdos plot_figure 工具生成的绘图脚本（工作目录内执行，只写 figure.png）。"""
import json

import matplotlib

matplotlib.use("Agg")
# 中文字体回退链（PRD 工具链清单：matplotlib 中文是已知坑）
matplotlib.rcParams["font.sans-serif"] = [
    "SimHei", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei", "sans-serif",
]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt

spec = json.loads({series_json!r})
fig, ax = plt.subplots(figsize=(8, 5))
kind = {args.kind!r}
for item in spec:
    values = item["values"]
    label = item["label"]
    if kind == "bar":
        ax.bar(range(len(values)), values, label=label)
    elif kind == "scatter":
        ax.scatter(range(len(values)), values, label=label)
    else:
        ax.plot(values, marker="o", label=label)
ax.set_title({args.title!r})
ax.set_xlabel({args.x_label!r})
ax.set_ylabel({args.y_label!r})
ax.grid(True, alpha=0.3)
if len(spec) > 1:
    ax.legend()
fig.savefig({_FIGURE_NAME!r}, dpi=150, bbox_inches="tight")
print({_FIGURE_NAME!r})
'''


def make_plot_figure(sandbox: Sandbox):
    """依赖注入：绑定沙箱实例的 plot_figure 执行器。"""

    async def execute(args: PlotFigureArgs, ctx: ToolContext) -> ToolResult:
        result = await sandbox.execute(build_plot_script(args), {}, ctx.work_dir)
        if result.timed_out or result.exit_code != 0:
            tail = (result.stderr or result.error or "").strip()
            return ToolResult(
                ok=False,
                error=ToolError(
                    code="TOOL_FAILED",
                    message="绘图脚本执行失败" + ("（超时）" if result.timed_out else f"：{tail[-600:]}"),
                ),
            )
        ref = _FIGURE_NAME
        if not validate_artifact_path(ctx.work_dir, ref):
            return ToolResult(
                ok=False, error=ToolError(code="TOOL_FAILED", message="产物路径越界，已拒绝引用")
            )
        return ToolResult(ok=True, summary=f"图表已生成：{ref}（{args.title}）", result_ref=ref)

    return execute
