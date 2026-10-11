"""stdio JSON-RPC 2.0 server（SP1-1 IPC 核心）。

从 stdin 逐行读 JSON-RPC 请求，经方法注册表分发，写响应到 stdout；
事件经 stdout NDJSON 流发射（用独立 sink 区分响应与事件由主进程按字段分流）。
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from engine.ipc.events import EventEmitter
from engine.ipc.limits import MAX_LINE_BYTES, LineGuard
from engine.ipc.rpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    RpcError,
    encode_error,
    encode_response,
    parse_request,
)
from engine.ipc.state import EngineState

MethodHandler = Callable[[dict], Awaitable[Any]]


class JsonRpcServer:
    """stdio JSON-RPC server：方法注册表 + 请求分发 + 优雅退出。"""

    def __init__(
        self, state: EngineState, events: EventEmitter, guard: LineGuard | None = None
    ) -> None:
        self._state = state
        self._events = events
        # 与事件发射器共用同一份计数（默认从 events 取），get_status.protocol 才是全链路视图
        self._guard = guard if guard is not None else getattr(events, "guard", LineGuard())
        self._methods: dict[str, MethodHandler] = {}
        self._stop = asyncio.Event()

    @property
    def guard(self) -> LineGuard:
        return self._guard

    def register(self, name: str, handler: MethodHandler) -> None:
        """注册一个 RPC 方法（6 个方法全部注册）。"""
        self._methods[name] = handler

    def stop(self) -> None:
        """触发优雅退出（SIGTERM 时调用，300ms 内落盘检查点标记）。"""
        self._stop.set()

    async def handle_line(self, line: str) -> str:
        """处理一行请求，返回一行响应；读写两侧都受契约单行上限约束（G25/W36）。"""
        if self._guard.exceeds(line):
            # 读向：超限请求行当场拒绝并计数，**不断链**（EC-S6）。上限管的是"引擎愿意解析并
            # 分派的单行规模"，不是 stdin 缓冲字节数——见 limits.py 模块头的边界说明。
            self._guard.note_oversized_in()
            return encode_error(
                None, INVALID_REQUEST,
                f"单行超过契约上限 {MAX_LINE_BYTES} 字节，已拒绝（EC-S6；计数见 get_status.protocol）",
            )
        try:
            req = parse_request(line)
        except RpcError as exc:
            return encode_error(None, exc.code, exc.message)

        handler = self._methods.get(req.method)
        if handler is None:
            return encode_error(req.id, METHOD_NOT_FOUND, f"未知方法：{req.method}")

        try:
            result = await handler(req.params)
            return self._bounded(encode_response(req.id, result), req.id)
        except ValueError as exc:
            return encode_error(req.id, INVALID_PARAMS, str(exc))
        except RpcError as exc:
            return encode_error(req.id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 收敛为 internal error
            return encode_error(req.id, INTERNAL_ERROR, f"内部错误：{exc}")

    def _bounded(self, response: str, request_id: Any) -> str:
        """写向兜底：响应超限不发出去（宿主 rpc.ts 会整行丢弃并记协议错误 = 静默丢结果），
        改发错误信封并计数。最现实的触发点是 `events_replay` 一次取太多事件。"""
        if not self._guard.exceeds(response):
            return response
        self._guard.note_oversized_out()
        return encode_error(
            request_id, INTERNAL_ERROR,
            f"响应超过契约单行上限 {MAX_LINE_BYTES} 字节，已拒绝返回；"
            "请调小 events_replay 的 limit 分页取（计数见 get_status.protocol）",
        )

    async def serve(self, initial_lines: list[str] | None = None) -> None:
        """主循环：逐行读 stdin，写响应到 stdout，直到 stop。

        initial_lines：首行密钥约定下"合法 JSON-RPC 首行"的回放队列
        （引擎先读首行判定密钥，若是请求行则在此先行执行，兼容无密钥调用方）。
        """
        loop = asyncio.get_event_loop()
        for line in initial_lines or []:
            line = line.strip()
            if not line:
                continue
            response = await self.handle_line(line)
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
        while not self._stop.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue
            response = await self.handle_line(line)
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
