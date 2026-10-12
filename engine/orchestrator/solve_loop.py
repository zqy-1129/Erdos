"""求解内循环（EN-LOOP W11）：模型驱动工具循环。

图结构（设计 §7.1）：
    START → decide ─(tool_calls)→ dispatch → decide（回注后再决策）
                  ─(无 tool_calls)→ finalize ─(校验过)→ END
    dispatch/finalize ─(修复/分发预算耗尽)→ handoff（awaiting_input）→ END

红线与语义：
- State 只存可序列化小对象（messages/results/计数），Key/句柄不入 State（开发文档 §5.1）；
- 副作用幂等（DEC-005）：dispatch 前 OperationLog.find——已完成则复用引用**不重放执行**；
  执行成功 record_done（first-wins）；
- 修复预算：工具失败/结果校验失败 repair_count+1，> max_repairs 或分发数 > max_dispatches
  → handoff（awaiting_input，复用既有门禁人工干预语义，不新造 interrupt——DEC-008）；
- 收敛硬条件：最终输出为严格 JSON {"results":[{"name","value","unit"?}]}，非空、含 name、
  数值有限（NaN/Infinity 直接拒绝——parse_constant），禁止凭空填数由 prompt 约束；
- 无工具能力端点不构建本循环（路由在 StagePipeline：tool_mode=stage_level 降级路径）。
"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypedDict

from engine.adapters.openai_compat import ChatMessage, ChatResult, ToolCall
from engine.ipc.throttle import DeltaThrottler
from engine.orchestrator.backend import END, build_loop_graph
from engine.orchestrator.operations import OperationLog, OperationRecord
from engine.sandbox.base import validate_artifact_path
from engine.tools import ToolRegistry
from engine.tools.base import TOOL_NOT_AVAILABLE, ToolContext

MAX_REPAIRS = 3  # 每次求解尝试的修复上限（开发文档 §5.3 / DEC-008）
MAX_DISPATCHES = 8  # 分发次数硬上限（防无界循环，预算红线）

SYSTEM_PROMPT = (
    "你是数学建模求解代理。可用工具由请求的 tools 参数给出；一切代码只能经 "
    "execute_code 在沙箱执行，禁止编造数值——数值必须来自工具执行结果。"
    "求解完成时，输出且仅输出一个 JSON 对象："
    '{"results": [{"name": "结果名", "value": 数值或字符串, "unit": "单位(可选)"}]}，'
    "value 禁止 NaN/Infinity；结果必须覆盖任务的全部必需小问。"
    "工具执行失败时，读取错误信息修正参数后重试，不要重复同一错误。"
)

_FINAL_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def artifact_sha256(work_dir: Path, result_ref: str | None) -> str | None:
    """引用产物的内容哈希（EC-T4）；result_ref 非文件或不存在 → None（按状态复用）。"""
    if not result_ref:
        return None
    path = work_dir / result_ref
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_constant(token: str) -> float:
    """json.loads parse_constant 钩子：NaN/Infinity 一律拒绝（收敛硬条件）。"""
    raise ValueError(f"结果含非法常量：{token}")


def parse_final_results(content: str) -> list[dict]:
    """从最终输出解析并校验 results（硬检查，失败抛 ValueError 由 finalize 捕获）。"""
    match = _FINAL_JSON_PATTERN.search(content or "")
    if match is None:
        raise ValueError("最终输出不含 JSON 对象")
    data = json.loads(match.group(0), parse_constant=_reject_constant)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        raise ValueError("results 必须为非空数组")
    for item in results:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            raise ValueError("results 项缺少 name")
        value = item.get("value")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("value 必须为有限数值")
        if value is None:
            raise ValueError("results 项缺少 value")
    return results


def _message_to_dict(m: ChatMessage) -> dict:
    """ChatMessage → JSON 可序列化 dict（State 存储）。"""
    out: dict = {"role": m.role}
    if m.content is not None:
        out["content"] = m.content
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments_json": tc.arguments_json} for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    return out


def _dict_to_message(d: dict) -> ChatMessage:
    """State 中的消息 dict → ChatMessage（LLM 端口入参）。"""
    tool_calls = tuple(
        ToolCall(id=tc["id"], name=tc["name"], arguments_json=tc.get("arguments_json", ""))
        for tc in d.get("tool_calls") or []
    )
    return ChatMessage(
        role=d["role"],
        content=d.get("content"),
        tool_calls=tool_calls or None,
        tool_call_id=d.get("tool_call_id"),
    )


class SolveState(TypedDict, total=False):
    """循环状态（全部 JSON 可序列化小对象）。"""

    task_id: str
    attempt: int
    exec_seq: int
    messages: list[dict]
    results: list[dict]
    repair_count: int
    dispatch_count: int
    # 其中"引擎宣告了自己还没实现的能力"的次数（G48 归因面）：占位工具 `search_references` 在
    # EN-19A 之前恒返回 NOT_AVAILABLE，它的失败既计 dispatch_count 又计 repair_count，
    # 于是终态可能被读成"模型/沙箱不行"。单列一个计数才说得出真实原因；**不改动任何预算语义**。
    unavailable_count: int
    status: str
    limitations: list[str]
    usage: dict
    isolation_mode: str  # 真正执行代码的沙箱自报的模式（DEC-006 证据链，不是调用方猜测）


# 通道名从 schema 注解导出：两套后端共用同一份声明，不会各写一遍再漂移
LOOP_CHANNELS = tuple(SolveState.__annotations__)


class SolveLLMPort(Protocol):
    """求解循环的 LLM 端口协议：携带 tools 的 chat（适配器经端口包装注入）。

    on_delta：token 增量回调（W15 流式；端口可忽略——FakeLLM/脚本化替身无需实现）。
    """

    async def __call__(
        self, messages: list[ChatMessage], tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult: ...


class SolveLoop:
    """模型驱动求解循环：decide → dispatch → finalize，预算耗尽转人工。"""

    def __init__(
        self,
        llm: SolveLLMPort,
        registry: ToolRegistry,
        operations: OperationLog,
        *,
        task_id: str,
        work_root,  # noqa: ANN001 - Path（任务工作目录父级，必填）
        attempt: int = 1,
        max_repairs: int = MAX_REPAIRS,
        max_dispatches: int = MAX_DISPATCHES,
        delta_sink: Callable[[str, str], None] | None = None,  # (task_id, delta) → model.delta
        impl: str | None = None,  # 编排后端（None=按 ERDOS_ORCH_IMPL；双轨比对显式指定）
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._operations = operations
        self._task_id = task_id
        self._attempt = attempt
        self._work_root = work_root
        self._max_repairs = max_repairs
        self._max_dispatches = max_dispatches
        self._delta_sink = delta_sink
        self._impl = impl
        self._backend = self._build()

    # ------------------------------------------------------------------
    async def run(self, task_prompt: str) -> dict:
        """执行循环至收敛或转人工；返回结构化 outcome（供 pipeline/事件消费）。"""
        # W15：每次 run 绑定独立节流器（50ms/256 字符合并 → model.delta 事件）
        sink = self._delta_sink
        throttle = (
            DeltaThrottler(emit=lambda text: sink(self._task_id, text))
            if sink is not None
            else None
        )
        self._throttle = throttle
        initial: SolveState = {
            "task_id": self._task_id,
            "attempt": self._attempt,
            "exec_seq": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ],
            "results": [],
            "repair_count": 0,
            "dispatch_count": 0,
            "unavailable_count": 0,
            "status": "running",
            "limitations": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
        }
        final: SolveState = await self._backend.run(initial)
        return {
            "status": final.get("status", "failed"),
            "results": final.get("results", []),
            "repair_count": final.get("repair_count", 0),
            "dispatch_count": final.get("dispatch_count", 0),
            "limitations": final.get("limitations", []),
            "usage": final.get("usage", {}),
            "isolation_mode": final.get("isolation_mode") or None,
        }

    # ------------------------------------------------------------------
    def _build(self):
        """装配循环图（decide/dispatch/finalize/handoff）；后端由 `ERDOS_ORCH_IMPL` 选择。"""
        return build_loop_graph(
            schema=SolveState,
            channels=LOOP_CHANNELS,
            entry="decide",
            nodes={
                "decide": self._decide,
                "dispatch": self._dispatch,
                "finalize": self._finalize,
                "handoff": self._handoff,
            },
            static={"handoff": END},
            conditional={
                "decide": (
                    self._route_after_decide,
                    {"dispatch": "dispatch", "finalize": "finalize"},
                ),
                "dispatch": (
                    self._route_after_budget,
                    {"decide": "decide", "handoff": "handoff"},
                ),
                "finalize": (
                    self._route_after_finalize,
                    {"decide": "decide", "handoff": "handoff", "done": END},
                ),
            },
            impl=self._impl,
        )

    def _route_after_finalize(self, state: SolveState) -> str:
        if state.get("status") == "succeeded":
            return "done"
        if state.get("repair_count", 0) > self._max_repairs:
            return "handoff"
        return "decide"

    def _route_after_decide(self, state: SolveState) -> str:
        last = state["messages"][-1]
        return "dispatch" if last.get("tool_calls") else "finalize"

    def _route_after_budget(self, state: SolveState) -> str:
        if state.get("repair_count", 0) > self._max_repairs:
            return "handoff"
        if state.get("dispatch_count", 0) > self._max_dispatches:
            return "handoff"
        return "decide"

    # ------------------------------------------------------------------
    async def _decide(self, state: SolveState) -> dict:
        """调用 LLM（带工具清单 + token 增量回调）；assistant 消息入栈。"""
        chat_messages = [_dict_to_message(m) for m in state["messages"]]
        on_delta = self._throttle.push if self._throttle is not None else None
        result = await self._llm(chat_messages, tools=self._registry.tool_payloads(), on_delta=on_delta)
        if self._throttle is not None:
            self._throttle.flush()  # 尾部增量不丢
        usage = dict(state.get("usage") or {})
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + result.usage.prompt_tokens
        usage["completion_tokens"] = usage.get("completion_tokens", 0) + result.usage.completion_tokens
        usage["calls"] = usage.get("calls", 0) + 1
        # 内循环是多次调用，"这一阶段的用量里有多少是猜的"不能压成一个布尔丢失：
        # 记估算次数（G35），审计时 `estimated_calls < calls` 才说明部分是厂商计量。
        if result.usage.estimated:
            usage["estimated_calls"] = usage.get("estimated_calls", 0) + 1
        return {
            "messages": state["messages"] + [_message_to_dict(
                ChatMessage(role="assistant", content=result.content, tool_calls=tuple(result.tool_calls or []) or None)
            )],
            "usage": usage,
        }

    def _artifact_intact(self, record: OperationRecord) -> bool:
        """EC-T4：引用产物完整性——带哈希的记录校验文件哈希，缺失/不一致视为损坏。

        无哈希记录（纯文本结果或回退引用）按状态复用（与既有语义一致）；
        引用越出工作目录（记录被篡改情形）同样视为损坏，不读工作目录外文件（AT-13）。
        """
        if record.result_sha256 is None:
            return True
        ref = record.result_ref or ""
        work_dir = self._work_root / self._task_id
        if not ref or not validate_artifact_path(work_dir, ref):
            return False
        path = work_dir / ref
        if not path.is_file():
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == record.result_sha256

    async def _dispatch(self, state: SolveState) -> dict:
        """分发 assistant 请求的全部工具调用（幂等：已完成执行复用引用不重放）。"""
        last = state["messages"][-1]
        messages = state["messages"]
        exec_seq = state.get("exec_seq", 0)
        repair_count = state.get("repair_count", 0)
        dispatch_count = state.get("dispatch_count", 0)
        unavailable_count = state.get("unavailable_count", 0)
        isolation_mode = state.get("isolation_mode", "")
        for call_dict in last.get("tool_calls") or []:
            call = ToolCall(
                id=call_dict["id"], name=call_dict["name"],
                arguments_json=call_dict.get("arguments_json", ""),
            )
            exec_seq += 1
            dispatch_count += 1
            done = self._operations.find(self._task_id, "solving", self._attempt, exec_seq)
            repair_reexec = False
            if done is not None and not self._artifact_intact(done):
                # EC-T4：引用产物缺失/哈希不一致 → 按损坏重执行（成功后刷新记录哈希）
                done = None
                repair_reexec = True
            if done is not None:
                # DEC-005：恢复路径——已完成执行不重放，复用引用
                observation = f"[系统] 复用已完成执行 #{exec_seq}（引用：{done.result_ref}）"
            else:
                work_dir = self._work_root / self._task_id
                ctx = ToolContext(task_id=self._task_id, stage="solving", work_dir=work_dir)
                result = await self._registry.dispatch(call, ctx)
                # 执行体自报的隔离模式：最后一次派发为准（同一循环内沙箱对象不变）
                isolation_mode = str(result.data.get("isolation_mode") or isolation_mode)
                self._operations.record_done(
                    self._task_id, "solving", self._attempt, exec_seq,
                    result.result_ref or f"tool:{call.name}:{call.id}",
                    result_sha256=artifact_sha256(work_dir, result.result_ref),
                    force=repair_reexec and result.ok,
                )
                if result.ok:
                    observation = result.summary or f"[ok] {result.result_ref or '执行完成'}"
                else:
                    repair_count += 1
                    if result.error is not None and result.error.code == TOOL_NOT_AVAILABLE:
                        # 与"模型写错了/沙箱挂了"分开记：这类失败无论重试多少次都不会成功
                        unavailable_count += 1
                    observation = f"[失败 {result.error.code}] {result.error.message}" if result.error else "[失败] 未知错误"
            messages = messages + [{
                "role": "tool", "content": observation, "tool_call_id": call.id,
            }]
        return {"messages": messages, "exec_seq": exec_seq,
                "repair_count": repair_count, "dispatch_count": dispatch_count,
                "unavailable_count": unavailable_count,
                "isolation_mode": isolation_mode}

    async def _finalize(self, state: SolveState) -> dict:
        """最终输出校验（硬检查）：通过 → succeeded；失败 → 回注修复。"""
        last = state["messages"][-1]
        try:
            results = parse_final_results(last.get("content") or "")
        except ValueError as exc:
            feedback = f"[系统] 结果校验失败：{exc}；请修正后重新输出最终 JSON（不得编造数值）。"
            return {
                "repair_count": state.get("repair_count", 0) + 1,
                "messages": state["messages"] + [{"role": "user", "content": feedback}],
            }
        return {"results": results, "status": "succeeded"}

    async def _handoff(self, state: SolveState) -> dict:
        """预算耗尽转人工：awaiting_input（复用既有门禁语义，保留现场）。"""
        note = (
            f"修复/分发预算耗尽（repair={state.get('repair_count', 0)}, "
            f"dispatch={state.get('dispatch_count', 0)}）"
        )
        unavailable = state.get("unavailable_count", 0)
        if unavailable:
            # 占位工具烧掉的派发**重试多少次都不会成功**：只写"预算耗尽"会把引擎的过度宣告
            # 报成模型或沙箱的问题（G48 归因面）。预算语义本批刻意不动，先把"是谁的问题"说清。
            note += (
                f"；其中 {unavailable} 次派发指向引擎尚未实现的能力（恒 NOT_AVAILABLE），"
                "非模型或沙箱故障"
            )
        return {
            "status": "awaiting_input",
            "limitations": state.get("limitations", []) + [note],
        }
