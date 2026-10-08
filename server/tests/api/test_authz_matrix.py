"""鉴权矩阵守护：每条 /v1 路由都必须声明策略，且实测行为与声明一致。

为什么存在：设计红线写的是"公开接口白名单（注册/登录/支付回调）"，其余一律要鉴权，
但这条约束此前只存在于文字里——没有任何测试盯住"新增端点忘了挂角色"。本矩阵把口径变成代码：
表里没有的路由直接失败（新端点必须显式归类），表里声明了却没生效的同样失败。

三类策略的实测口径：
- public：无凭证不得返回 401/403（本来就该匿名可达，如注册/登录/支付回调/健康检查）；
- user：无凭证必须 401；带合法用户凭证不得 401/403；
- admin：无凭证必须 401；带普通用户凭证必须 403（越权面）。

只探测 GET 做"不得拒绝"的正向断言：POST 会真的产生业务副作用，流式端点会挂住测试。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.middleware import PUBLIC_PATHS
from app.core.config import Settings
from app.main import create_app
from conftest import StaticIntrospector

# (method, path) -> public | user | admin。public 即设计文档的"公开接口白名单"。
POLICY: dict[tuple[str, str], str] = {
    # ---- 公开：注册/登录/令牌/找回/健康/商品目录/支付回调/验证码 ----
    ("GET", "/v1/health"): "public",
    ("GET", "/v1/auth/jwks"): "public",
    ("POST", "/v1/auth/register"): "public",
    ("POST", "/v1/auth/login"): "public",
    ("POST", "/v1/auth/refresh"): "public",
    ("POST", "/v1/auth/logout"): "public",
    ("POST", "/v1/auth/password/reset/request"): "public",
    ("POST", "/v1/auth/password/reset/confirm"): "public",
    ("GET", "/v1/billing/products"): "public",
    ("POST", "/v1/billing/callbacks/payment"): "public",  # HMAC 验签 fail-closed
    ("POST", "/v1/notifications/verification-code"): "public",  # 限流 60s/日10
    # 匿名摄入是既有跨端契约（登录前事件靠设备级 distinct_id 归因）；收紧会静默打断
    # 客户端 outbox 补报，伪造治理口径归 DEC-028，见 contracts/openapi.yaml 同条描述
    ("POST", "/v1/telemetry/events"): "public",
    # ---- 登录用户 ----
    ("GET", "/v1/account/profile"): "user",
    ("POST", "/v1/account/password"): "user",
    ("GET", "/v1/account/devices"): "user",
    ("DELETE", "/v1/account"): "user",
    ("GET", "/v1/points/balance"): "user",
    ("GET", "/v1/points/ledger"): "user",
    ("GET", "/v1/points/ledger/export"): "user",
    ("POST", "/v1/points/reserve"): "user",
    ("POST", "/v1/points/confirm"): "user",
    ("POST", "/v1/points/refund"): "user",
    ("POST", "/v1/points/offline-sync"): "user",
    ("POST", "/v1/billing/orders"): "user",
    ("GET", "/v1/billing/orders/{order_id}"): "user",
    ("POST", "/v1/billing/orders/{order_id}/refund"): "user",
    ("GET", "/v1/billing/subscription"): "user",
    ("POST", "/v1/billing/subscription/monthly-grant"): "user",
    ("GET", "/v1/entitlements/snapshot"): "user",
    ("GET", "/v1/content/problems"): "user",
    ("GET", "/v1/content/templates"): "user",
    ("GET", "/v1/content/cases"): "user",
    ("GET", "/v1/content/manifest"): "user",
    ("POST", "/v1/presence/heartbeat"): "user",
    # ---- 管理端（admin 或 operator）----
    ("GET", "/v1/admin/dashboard/users/online"): "admin",
    ("GET", "/v1/admin/dashboard/users/total"): "admin",
    ("GET", "/v1/admin/dashboard/users/trend"): "admin",
    ("GET", "/v1/admin/dashboard/online/trend"): "admin",
    ("GET", "/v1/admin/dashboard/events"): "admin",
    ("GET", "/v1/admin/dashboard/stream"): "admin",
    ("GET", "/v1/admin/dashboard/points/summary"): "admin",
    ("GET", "/v1/admin/dashboard/points/trend"): "admin",
    ("GET", "/v1/admin/dashboard/points/distribution"): "admin",
    ("GET", "/v1/admin/dashboard/billing/summary"): "admin",
    ("GET", "/v1/admin/dashboard/billing/revenue"): "admin",
    ("GET", "/v1/admin/dashboard/billing/products"): "admin",
    ("GET", "/v1/admin/monitoring/overview"): "admin",
    ("GET", "/v1/admin/monitoring/trend"): "admin",
    ("GET", "/v1/admin/monitoring/alerts"): "admin",
    ("GET", "/v1/admin/monitoring/metrics-spec"): "admin",
    ("GET", "/v1/analytics/funnel"): "admin",
    ("GET", "/v1/analytics/dashboard"): "admin",
    ("POST", "/v1/content/problems"): "admin",
    ("POST", "/v1/content/templates"): "admin",
    ("POST", "/v1/content/cases"): "admin",
    ("POST", "/v1/scheduler/monthly-grant"): "admin",
    ("POST", "/v1/scheduler/expire-subscriptions"): "admin",
    ("POST", "/v1/scheduler/reconcile"): "admin",
    ("POST", "/v1/billing/orders/reconcile"): "admin",
    ("POST", "/v1/audit/events"): "admin",
}

# 路径参数占位：只为让请求打到真实路由上（404/400 都算"未被拒绝"）
PATH_SAMPLES = {"order_id": "00000000-0000-0000-0000-000000000000"}

# 正向"不得拒绝"探测的豁免：SSE 在 ASGITransport 下永不结束（会挂死测试），
# 且 httpx 的 ASGITransport 会缓冲整响应，超时也救不回来。角色拒绝仍照常探测。
SKIP_OK_PROBE = {"/v1/admin/dashboard/stream"}


def _declared_routes(app) -> set[tuple[str, str]]:
    schema = app.openapi()
    out: set[tuple[str, str]] = set()
    for path, item in schema["paths"].items():
        for method in item:
            if method in {"get", "post", "put", "patch", "delete"}:
                out.add((method.upper(), path))
    return out


def _make_app(settings: Settings, roles: tuple[str, ...] | None):
    """建表 + 强制鉴权的应用实例（矩阵要打到真实路由上，空库会让管理端报表先 500）。"""
    enforced = settings.model_copy(update={"auth_enforce": True})
    app = create_app(enforced, introspector=StaticIntrospector(roles or ()))
    return app


async def _client(app):
    from app.repository.models import Base

    engine = app.state.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def anon_client(settings: Settings):
    """开启强制鉴权、凭证角色的客户端（模拟普通登录用户与匿名访客）。"""
    app = _make_app(settings, ())
    async with await _client(app) as c:
        yield c
    await app.state.engine.dispose()


@pytest.fixture
async def user_client(settings: Settings):
    app = _make_app(settings, ("user",))
    async with await _client(app) as c:
        yield c
    await app.state.engine.dispose()


@pytest.fixture
async def role_client(settings: Settings):
    app = _make_app(settings, ("admin", "operator"))
    async with await _client(app) as c:
        yield c
    await app.state.engine.dispose()


def _url(path: str) -> str:
    return path.format(**PATH_SAMPLES)


def test_every_route_declares_a_policy() -> None:
    """新增/删除端点必须同步本表：漏声明策略直接失败（防止"忘了挂角色"的新接口）。"""
    app = create_app(Settings(env="test", database_url="sqlite+aiosqlite:///:memory:"))
    actual = _declared_routes(app)
    declared = set(POLICY)
    missing = sorted(actual - declared)
    stale = sorted(declared - actual)
    assert not missing, f"未声明鉴权策略的路由：{missing}"
    assert not stale, f"鉴权矩阵里有已不存在的路由（请删除）：{stale}"


def test_public_tier_matches_middleware_whitelist() -> None:
    """策略表的 public 档必须与中间件白名单逐条相等。

    enforce=True 下白名单外一律 401：两边不一致就意味着"文档说公开、实际拦死"
    （注册/支付回调/公钥发布都踩过这一条）或反过来漏放。
    """
    policy_public = {path for (_method, path), tier in POLICY.items() if tier == "public"}
    assert policy_public == set(PUBLIC_PATHS) - {"/metrics"}, (
        f"只在策略表：{sorted(policy_public - set(PUBLIC_PATHS))}；"
        f"只在白名单：{sorted(set(PUBLIC_PATHS) - policy_public - {'/metrics'})}"
    )


async def test_anonymous_cannot_reach_protected_routes(anon_client) -> None:
    """无凭证访问 user/admin 路由必须 401；公开路由不得被拒。"""
    bad: list[str] = []
    for (method, path), tier in sorted(POLICY.items()):
        if tier == "public":
            continue
        resp = await anon_client.request(method, _url(path))
        if resp.status_code != 401:
            bad.append(f"{method} {path} -> {resp.status_code}（期望 401）")
    assert not bad, "匿名可达的受保护端点：\n" + "\n".join(bad)


async def test_public_routes_stay_open_without_credentials(anon_client) -> None:
    bad: list[str] = []
    for (method, path), tier in sorted(POLICY.items()):
        if tier != "public":
            continue
        resp = await anon_client.request(method, _url(path))
        if resp.status_code in (401, 403):
            bad.append(f"{method} {path} -> {resp.status_code}")
    assert not bad, "公开白名单端点被鉴权挡下（口径漂移）：\n" + "\n".join(bad)


async def test_plain_user_cannot_reach_admin_routes(user_client) -> None:
    """越权面：普通用户拿 admin/operator 路由必须 403，而不是 200。"""
    bad: list[str] = []
    for (method, path), tier in sorted(POLICY.items()):
        if tier != "admin":
            continue
        resp = await user_client.request(
            method, _url(path), headers={"Authorization": "Bearer someone"}
        )
        if resp.status_code != 403:
            bad.append(f"{method} {path} -> {resp.status_code}（期望 403）")
    assert not bad, "普通用户可访问管理端：\n" + "\n".join(bad)


async def test_user_tier_accepts_authenticated_subject(user_client) -> None:
    """只探 GET：确认 user 档没被过度收紧（401/403 即为回归）。"""
    bad: list[str] = []
    for (method, path), tier in sorted(POLICY.items()):
        if tier != "user" or method != "GET" or path in SKIP_OK_PROBE:
            continue
        resp = await user_client.request(
            method, _url(path), headers={"Authorization": "Bearer u1"}
        )
        if resp.status_code in (401, 403):
            bad.append(f"{method} {path} -> {resp.status_code}")
    assert not bad, "登录用户被拒的 user 档端点：\n" + "\n".join(bad)


async def test_admin_tier_accepts_admin_role(role_client) -> None:
    bad: list[str] = []
    for (method, path), tier in sorted(POLICY.items()):
        if tier != "admin" or method != "GET" or path in SKIP_OK_PROBE:
            continue
        resp = await role_client.request(
            method, _url(path), headers={"Authorization": "Bearer admin"}
        )
        if resp.status_code in (401, 403):
            bad.append(f"{method} {path} -> {resp.status_code}")
    assert not bad, "管理员被拒的 admin 档端点：\n" + "\n".join(bad)
