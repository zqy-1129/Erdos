"""ToolRegistry（EN-REG W9）：allowlist 分发 + 参数校验 + 事件回流 + 留痕。

dispatch 语义（《任务执行手册》W9）：
- 未注册工具 → TOOL_UNKNOWN（含可用工具清单），回注模型而非抛异常 + 留痕；
- 参数校验失败 → TOOL_ARGS_INVALID（错误附类型与位置，不含输入明文——防注入外泄）；
- 执行异常收敛为 TOOL_FAILED 结构化错误（不炸编排循环）；
- 每次 dispatch 发 tool.call（执行前）与 tool.result（执行后）事件，并写 tool_call 留痕；
- args_summary/summary 经脱敏（sk-密钥/Bearer 头掩码）与截断（512/2000）。
"""

import json
import re
import time
from typing import Any

from pydantic import ValidationError

from engine.ipc.events import EventEmitter
from engine.tools.base import ToolCallLike, ToolContext, ToolError, ToolResult, ToolSpec
from engine.trail.recorder import TrailRecorder

ARGS_SUMMARY_LIMIT = 512
SUMMARY_LIMIT = 2000
ERROR_LIMIT = 1024

# 凭据形状掩码（值级脱敏；键级脱敏由 trail redact_sensitive 承担）
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{6,}"), "sk-***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{6,}", re.IGNORECASE), "Bearer ***"),
)


def mask_secrets(text: str) -> str:
    """值级脱敏：掩码 sk-* / Bearer * 形状凭据（事件与摘要出口共用）。"""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clip(text: str, limit: int) -> str:
    """截断到上限（供 summary/args_summary/error 出口统一使用）。"""
    return text if len(text) <= limit else text[:limit]


class ToolRegistry:
    """可插拔工具注册表 = allowlist（DEC-026）：注册即白名单，未注册即拒绝。"""

    def __init__(
        self,
        events: EventEmitter | None = None,
        trail: TrailRecorder | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._events = events
        self._trail = trail

    def register(self, spec: ToolSpec) -> None:
        """注册工具（重复名拒绝——防覆盖 allowlist）。"""
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        """已注册的工具规格（只读快照）。

        与 `names()` 同一类自省口：产物类别 `artifact_kind` 是注册时声明的承诺，
        声明是否与契约 enum 一致需要机器可比对——否则又是"注释里说同源"。
        """
        return list(self._tools.values())

    def tool_payloads(self) -> list[dict]:
        """OpenAI tools 数组（parameters 由 args_model 单源生成）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameter_schema(),
                },
            }
            for spec in self._tools.values()
        ]

    async def dispatch(self, call: ToolCallLike, ctx: ToolContext) -> ToolResult:
        """分发一次工具调用：校验 → 执行 → 事件/留痕 → ToolResult（永不抛业务异常）。"""
        spec = self._tools.get(call.name)
        args_summary = clip(mask_secrets(str(call.arguments_json)), ARGS_SUMMARY_LIMIT)

        if self._events is not None:
            self._events.emit(
                "tool.call",
                task_id=ctx.task_id,
                stage=ctx.stage,
                call_id=call.id,
                tool=call.name,
                args_summary=args_summary,
            )

        result = await self._execute(spec, call, ctx) if spec is not None else ToolResult(
            ok=False,
            error=ToolError(
                code="TOOL_UNKNOWN",
                message=f"未知工具：{call.name}；可用工具：{', '.join(self.names())}",
            ),
        )

        if self._trail is not None:
            detail: dict[str, Any] = {
                "call_id": call.id,
                "args_summary": args_summary,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
                "error": result.error.model_dump() if result.error else None,
            }
            # 产物登记结果并进同一条 tool_call：留痕条数 = 调用次数。多写一条会让
            # "这题到底调了几次工具"这种复核问题永远答错（审计是逐条数着看的）。
            detail.update(self._register_artifact(spec, result, ctx, self._trail))
            self._trail.record_tool_call(ctx.task_id, ctx.stage, call.name, detail)
        if self._events is not None:
            self._events.emit(
                "tool.result",
                task_id=ctx.task_id,
                call_id=call.id,
                tool=call.name,
                ok=result.ok,
                duration_ms=result.duration_ms,
                result_ref=result.result_ref,
                error=clip(result.error.message, ERROR_LIMIT) if result.error else None,
            )
        return result

    def _register_artifact(
        self, spec: ToolSpec | None, result: ToolResult, ctx: ToolContext, trail: TrailRecorder
    ) -> dict:
        """产物登记（W23/G41 的留痕侧）：返回要并进 tool_call 明细的标记，永不抛错。

        - 只有工具自己声明了 `artifact_kind` 才登记：`execute_code` 可能产出任意文件，引擎侧不可
          枚举，硬猜 kind 就是编造证据；
        - **文件真在盘上才登记**：`record_artifact` 会算 sha256，登记一个不存在的文件等于伪造
          支撑材料；
        - 声称有产物却盘上没有（沙箱替身、失败分支都可能这样）→ 回 `artifact_missing`，
          让"工具说它画了图"与"图上确实有图"在审计里是两件可区分的事。
        """
        if spec is None or spec.artifact_kind is None or not result.ok or not result.result_ref:
            return {}
        path = ctx.work_dir / result.result_ref
        if not path.exists():
            return {
                "artifact_missing": True,
                "expected_kind": spec.artifact_kind,
                "expected_path": result.result_ref,
            }
        trail.record_artifact(ctx.task_id, ctx.stage, spec.artifact_kind, path)
        return {"artifact_kind": spec.artifact_kind, "artifact_path": result.result_ref}

    async def _execute(self, spec: ToolSpec, call: ToolCallLike, ctx: ToolContext) -> ToolResult:
        """参数校验 + 执行器调用；异常收敛为结构化错误。"""
        started = time.monotonic()
        try:
            args = spec.args_model.model_validate_json(str(call.arguments_json))
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            detail = "参数不是合法 JSON"
            if isinstance(exc, ValidationError):
                # 只带类型与位置，不带 input 明文（防注入内容外泄）
                detail = "; ".join(
                    f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['type']}" for e in exc.errors()
                )
            return ToolResult(
                ok=False,
                error=ToolError(code="TOOL_ARGS_INVALID", message=f"参数校验失败：{detail}"),
            )
        try:
            result = await spec.executor(args, ctx)
        except Exception as exc:  # noqa: BLE001 - 工具异常收敛为结构化错误回注（EC-T 边界）
            result = ToolResult(
                ok=False,
                error=ToolError(
                    code="TOOL_FAILED",
                    message=clip(mask_secrets(f"{type(exc).__name__}: {exc}"), ERROR_LIMIT),
                ),
            )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.summary = clip(mask_secrets(result.summary), SUMMARY_LIMIT)
        return result
