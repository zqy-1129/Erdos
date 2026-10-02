"""JSON-RPC 解析与编码单元测试（SP1-1）。"""

import json

import pytest

from engine.ipc.rpc import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcError,
    encode_error,
    encode_response,
    parse_request,
)


def test_parse_valid_request() -> None:
    req = parse_request('{"jsonrpc":"2.0","id":1,"method":"get_status","params":{}}')
    assert req.id == 1
    assert req.method == "get_status"
    assert req.params == {}


def test_parse_missing_jsonrpc() -> None:
    with pytest.raises(RpcError) as ei:
        parse_request('{"id":1,"method":"get_status"}')
    assert ei.value.code == INVALID_REQUEST


def test_parse_invalid_json() -> None:
    with pytest.raises(RpcError) as ei:
        parse_request("not json")
    assert ei.value.code == PARSE_ERROR


def test_parse_params_none_becomes_empty_dict() -> None:
    req = parse_request('{"jsonrpc":"2.0","id":1,"method":"get_status","params":null}')
    assert req.params == {}


def test_encode_response_and_error() -> None:
    resp = encode_response(1, {"engine": "idle"})
    assert json.loads(resp)["result"] == {"engine": "idle"}
    err = encode_error(1, METHOD_NOT_FOUND, "未知方法")
    obj = json.loads(err)
    assert obj["error"]["code"] == METHOD_NOT_FOUND
