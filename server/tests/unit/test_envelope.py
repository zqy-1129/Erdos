"""统一响应信封测试。"""

from datetime import UTC, datetime

from app.core.envelope import Envelope, fail, ok
from app.core.errors import CONFLICT, OK


def test_ok_envelope_shape() -> None:
    payload = {"n": 1}
    env = ok(payload, "req-001")
    assert env.code == OK.code == 0
    assert env.message == OK.message
    assert env.data == payload
    assert env.detail is None
    assert env.request_id == "req-001"
    assert env.timestamp.tzinfo is not None


def test_fail_envelope_shape() -> None:
    env = fail(CONFLICT, "req-002", detail="重复请求")
    assert env.code == 40901
    assert env.message == CONFLICT.message
    assert env.data is None
    assert env.detail == "重复请求"
    assert env.request_id == "req-002"


def test_envelope_json_serialization() -> None:
    env = ok(None, "r1", timestamp=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    payload = env.model_dump(mode="json")
    assert isinstance(payload["timestamp"], str)
    assert payload["code"] == 0
    assert "request_id" in payload


def test_envelope_is_pydantic_model() -> None:
    assert issubclass(Envelope, object) and hasattr(Envelope, "model_fields")