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
    return RpcRequest(id=obj.get("id"), method=method, params=params)


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
