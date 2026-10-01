"""健康检查接口测试。"""

import pytest
from sqlalchemy.exc import OperationalError

from app import __version__
from app.api.v1.health import get_health
from app.core.errors import DB_UNAVAILABLE, AppError


async def test_health_ok(client) -> None:
    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == "成功"
    assert body["data"] == {"status": "ok", "version": __version__}
    assert body["request_id"] == resp.headers["X-Request-Id"]


async def test_health_db_down_maps_503() -> None:
    class BrokenSession:
        async def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    with pytest.raises(AppError) as exc_info:
        await get_health(BrokenSession())  # type: ignore[arg-type]
    assert exc_info.value.spec is DB_UNAVAILABLE
    assert exc_info.value.spec.http_status == 503