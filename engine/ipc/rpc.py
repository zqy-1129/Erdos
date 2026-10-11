"""JSON-RPC 2.0 消息定义与解析（SP1-1 IPC 骨架）。"""

import json
from dataclasses import dataclass
from typing import Any

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# 服务端业务错误码（开发文档 §7.4 错误码清单）
SCHEMA_UNSUPPORTED = -32000  # 协议版本协商失败（initialize 不匹配，拒发任务）


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """一条 JSON-RPC 请求。"""

    id: Any
    method: str
    params: dict


class RpcError(Exception):
    """带 JSON-RPC 错误码的业务异常。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_request(line: str) -> RpcRequest:
    """解析一行 NDJSON 为 RpcRequest；格式非法抛 RpcError。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RpcError(PARSE_ERROR, "JSON 解析失败") from exc
    if not isinstance(obj, dict):
        raise RpcError(INVALID_REQUEST, "请求必须是对象")
    if obj.get("jsonrpc") != "2.0":
        raise RpcError(INVALID_REQUEST, "jsonrpc 必须为 2.0")
    method = obj.get("method")
    if not isinstance(method, str):
        raise RpcError(INVALID_REQUEST, "缺少 method")
    params = obj.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params 必须是对象")
    request_id = obj.get("id")
    # JSON-RPC 2.0 只允许 id 为 String / Number / Null。放行对象/数组/布尔的后果不是"不规范"这么轻：
    # 错误响应要把 id 原样回显，于是回显变成放大——一条**读向已放行**的请求行能让写向产出一行超限，
    # 宿主按协议错误整行丢弃（engine-host/rpc.ts:99），该请求就永远没有回应。
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int, float))
    ):
        raise RpcError(INVALID_REQUEST, "id 必须是字符串、数字或 null")
    return RpcRequest(id=request_id, method=method, params=params)


def encode_response(request_id: Any, result: Any) -> str:
    """编码成功响应为一行 NDJSON。"""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": result},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def encode_error(request_id: Any, code: int, message: str) -> str:
    """编码错误响应为一行 NDJSON。"""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
