"""网关中间件链：request_id 追踪 -> 指标 -> 限流 -> 鉴权透传（执行顺序）。

全部为纯 ASGI 中间件（不基于 BaseHTTPMiddleware）：

- 兼容流式响应（SSE 看板实时流不会被缓冲挂起）；
- 拒绝类中间件（限流/鉴权强制）直接以统一信封 JSON 响应，状态码真实可观测。

执行顺序（注册顺序对应的包裹关系）：RequestId -> Metrics -> RateLimit -> Auth -> 路由。
"""

import json
import math
import re
import time
import uuid
from collections.abc import Iterable

from fastapi.encoders import jsonable_encoder
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.envelope import Envelope, fail
from app.core.errors import RATE_LIMITED, UNAUTHENTICATED
from app.core.logging import request_id_var
from app.infra.auth import TokenIntrospector
from app.infra.metrics import (
    REQUEST_DURATION,
    REQUEST_IN_FLIGHT,
    REQUEST_TOTAL,
    route_path_label,
)
from app.infra.rate_limit import RateLimiter, SlidingWindowRateLimiter, client_ip_key

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{7,63}$")


def build_rate_limits(settings: Settings) -> list[tuple[str, RateLimiter]]:
    """按配置构造路径限流规则（长前缀优先匹配，兜底 default）。"""
    rules: list[tuple[str, RateLimiter]] = [
        (
            "/v1/audit",
            SlidingWindowRateLimiter(
                settings.audit_rate_limit_requests, settings.rate_limit_window_seconds
            ),
        ),
        (
            "",
            SlidingWindowRateLimiter(
                settings.rate_limit_requests, settings.rate_limit_window_seconds
            ),
        ),
    ]
    return sorted(rules, key=lambda item: len(item[0]), reverse=True)


class RequestIdMiddleware:
    """request_id 注入与回显（异常路径信封同样可追踪）。"""

    def __init__(self, app: ASGIApp, header_name: str = "X-Request-Id") -> None:
        self._app = app
        self._header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        incoming = _scope_header(scope, self._header_name)
        request_id = incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        # 写入 scope state：异常路径（全局处理器构造信封时）contextvar 可能已复位
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(self._header_name, request_id)
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)


class MetricsMiddleware:
    """网关指标采集：流式响应在流结束（最后一个 body 块）时记录真实状态码。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method", "?"))
        recorded = False

        def record(status: str) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            path = route_path_label(scope)
            REQUEST_DURATION.labels(method, path).observe(time.perf_counter() - start)
            REQUEST_TOTAL.labels(method, path, status).inc()
            app = scope.get("app")
            collector = getattr(app.state, "monitoring", None) if app is not None else None
            if collector is not None:
                collector.record_http(path, time.perf_counter() - start, int(status))

        start = time.perf_counter()
        REQUEST_IN_FLIGHT.inc()
        start_status = "200"

        async def send_wrapper(message: Message) -> None:
            nonlocal start_status
            if message["type"] == "http.response.start":
                start_status = str(message["status"])
            elif message["type"] == "http.response.body":
                await send(message)
                if not message.get("more_body", False):
                    record(start_status)  # 流结束（SSE 末块）才计时/计数
                return
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except BaseException:
            record("500")  # 未走响应 body 的异常（正常业务异常经异常处理器仍走 body 路径）
            raise
        finally:
            if not recorded:
                record(start_status)
            REQUEST_IN_FLIGHT.dec()


class RateLimitMiddleware:
    """按 IP 的路径规则限流；触发时返回 429 信封 + Retry-After。"""

    _SKIP_PREFIXES = ("/metrics", "/docs", "/redoc", "/openapi.json", "/admin")

    def __init__(self, app: ASGIApp, rules: Iterable[tuple[str, RateLimiter]]) -> None:
        self._app = app
        self._rules = list(rules)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if any(path.startswith(p) for p in self._SKIP_PREFIXES):
            await self._app(scope, receive, send)
            return
        limiter = self._match_limiter(path)
        if limiter is not None and not limiter.allow(client_ip_key(_scope_client_ip(scope))):
            retry_after = math.ceil(limiter.retry_after_seconds(client_ip_key(_scope_client_ip(scope))))
            await _send_envelope_json(
                send,
                429,
                fail(RATE_LIMITED, request_id_var.get(), detail="触发限流，请稍后重试"),
                extra_headers=[(b"retry-after", str(retry_after).encode("ascii"))],
            )
            return
        await self._app(scope, receive, send)

    def _match_limiter(self, path: str) -> RateLimiter | None:
        for prefix, limiter in self._rules:
            if path.startswith(prefix):
                return limiter
        return None


class AuthPassthroughMiddleware:
    """鉴权透传：解析 Bearer -> scope state 中的 principal。

    开启 enforce 时，缺失凭证直接以 401 信封拒绝（SP2-1 默认关闭）。
    """

    def __init__(
        self,
        app: ASGIApp,
        introspector: TokenIntrospector,
        *,
        enforce: bool = False,
    ) -> None:
        self._app = app
        self._introspector = introspector
        self._enforce = enforce

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        header = _scope_header(scope, "authorization")
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer" and token:
            principal = await self._introspector.introspect(token)
            scope.setdefault("state", {})["principal"] = principal
        if self._enforce and not scope.get("state", {}).get("principal"):
            await _send_envelope_json(send, 401, fail(UNAUTHENTICATED, request_id_var.get()))
            return
        await self._app(scope, receive, send)


def _scope_header(scope: Scope, name: str) -> str:
    """读取 scope 请求头（键为小写字节）。"""
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
    return ""


def _scope_client_ip(scope: Scope) -> str | None:
    forwarded = _scope_header(scope, "x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    client = scope.get("client")
    return str(client[0]) if client else None


async def _send_envelope_json(
    send: Send,
    status_code: int,
    envelope: Envelope[None],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """发送统一信封 JSON 响应（拒绝类中间件用）。"""
    body = json.dumps(jsonable_encoder(envelope), ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})