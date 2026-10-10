"""权益与内容接口集成测试（SP2-6）：快照签发 + 内容读取 + 管理侧 CRUD。

test 环境用 DevTokenIntrospector：Bearer <subject> 透传为请求主体。
admin 角色通过 admin_client 夹具（StaticIntrospector 注入 admin/operator）。
"""

from app.repository.models import Problem
from app.repository.uow import UnitOfWork


async def _seed_public_problem(client) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        uow.session.add(
            Problem(
                business_id="cumcm-2024-A", competition="cumcm", year=2024,
                problem_code="A", title="示例题", tags=[], prompt_zh="题干",
                visibility="public",
            )
        )
        await uow.session.flush()


async def test_snapshot_endpoint(client) -> None:
    """快照签发接口：返回签名快照。"""
    resp = await client.get(
        "/v1/entitlements/snapshot", headers={"Authorization": "Bearer u1"}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["payload"]["subscribed"] is False
    assert data["signature"]  # 有签名
    assert data["key_version"]


async def test_content_public_visible_to_non_member(client) -> None:
    """非会员可读 public 真题。"""
    await _seed_public_problem(client)
    resp = await client.get(
        "/v1/content/problems", headers={"Authorization": "Bearer u1"}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["business_id"] == "cumcm-2024-A"


async def test_cases_empty_for_non_member(client) -> None:
    """非会员案例列表为空。"""
    resp = await client.get(
        "/v1/content/cases", headers={"Authorization": "Bearer u1"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_manifest_endpoint(client) -> None:
    """manifest 需登录凭证（含 oss_key+sha256 的内容目录不得匿名枚举）；初始为空列表。"""
    anon = await client.get("/v1/content/manifest")
    assert anon.status_code == 401

    resp = await client.get(
        "/v1/content/manifest", headers={"Authorization": "Bearer u1"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_content_crud_requires_admin(client, admin_client) -> None:
    """内容 CRUD 需 admin：普通用户 403，admin 成功。"""
    body = {
        "business_id": "cumcm-2024-C",
        "competition": "cumcm",
        "year": 2024,
        "problem_code": "C",
        "title": "会员题",
        "prompt_zh": "题干",
        "visibility": "member",
    }
    # 普通用户 403
    r_denied = await client.post(
        "/v1/content/problems", json=body, headers={"Authorization": "Bearer u1"}
    )
    assert r_denied.status_code == 403

    # admin 成功
    r_ok = await admin_client.post(
        "/v1/content/problems", json=body, headers={"Authorization": "Bearer admin"}
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["data"]["business_id"] == "cumcm-2024-C"


async def test_case_upsert_with_compliance(admin_client) -> None:
    """admin 建案例：强制携带合规声明。"""
    body = {
        "business_id": "case-1",
        "problem_id": "cumcm-2024-A",
        "title": "案例",
        "award": "国一",
        "oss_key": "oss/case.pdf",
        "sha256": "c" * 64,
        "compliance_note": "仅作方法参照，禁止大段抄袭",
    }
    r = await admin_client.post(
        "/v1/content/cases", json=body, headers={"Authorization": "Bearer admin"}
    )
    assert r.status_code == 200
    assert r.json()["data"]["compliance_note"] == "仅作方法参照，禁止大段抄袭"


async def test_snapshot_requires_auth(client) -> None:
    """快照需登录态。"""
    assert (await client.get("/v1/entitlements/snapshot")).status_code == 401
