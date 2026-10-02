"""stdio JSON-RPC 2.0 server（SP1-1 IPC 核心）。

从 stdin 逐行读 JSON-RPC 请求，经方法注册表分发，写响应到 stdout；
事件经 stdout NDJSON 流发射（用独立 sink 区分响应与事件由主进程按字段分流）。
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from engine.ipc.events import EventEmitter
from engine.ipc.rpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
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

    def __init__(self, state: EngineState, events: EventEmitter) -> None:
        self._state = state
        self._events = events
        self._methods: dict[str, MethodHandler] = {}
        self._stop = asyncio.Event()

    def register(self, name: str, handler: MethodHandler) -> None:
        """注册一个 RPC 方法（6 个方法全部注册）。"""
        self._methods[name] = handler

    def stop(self) -> None:
        """触发优雅退出（SIGTERM 时调用，300ms 内落盘检查点标记）。"""
        self._stop.set()

    async def handle_line(self, line: str) -> str:
        """处理一行请求，返回一行响应。"""
        try:
            req = parse_request(line)
        except RpcError as exc:
            return encode_error(None, exc.code, exc.message)

        handler = self._methods.get(req.method)
        if handler is None:
            return encode_error(req.id, METHOD_NOT_FOUND, f"未知方法：{req.method}")

        try:
            result = await handler(req.params)
            return encode_response(req.id, result)
        except ValueError as exc:
            return encode_error(req.id, INVALID_PARAMS, str(exc))
        except RpcError as exc:
            return encode_error(req.id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 收敛为 internal error
            return encode_error(req.id, INTERNAL_ERROR, f"内部错误：{exc}")

    async def serve(self) -> None:
        """主循环：逐行读 stdin，写响应到 stdout，直到 stop。"""
        loop = asyncio.get_event_loop()
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
