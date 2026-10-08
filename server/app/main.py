"""应用装配入口：FastAPI 工厂（分层装配 + 网关中间件 + 全局异常处理）。"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.handlers import (
    app_error_handler,
    http_exception_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.api.middleware import (
    AuthPassthroughMiddleware,
    MetricsMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
    build_rate_limits,
)
from app.api.router import api_router
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger, setup_logging
from app.domain.account.reset import PasswordResetService
from app.domain.account.service import PasswordPolicy
from app.infra.alert_outlet import BrokerAlertOutlet
from app.infra.alert_webhook import AlertWebhookDispatcher
from app.infra.auth import (
    BcryptPasswordHasher,
    DevCredentialVerifier,
    DevTokenIntrospector,
    Ed25519LicenseSigner,
    Ed25519TokenManager,
    FallbackJwtIntrospector,
    JwtTokenIntrospector,
    LogResetNotifier,
    SqlCredentialVerifier,
    TokenIntrospector,
    build_signing_keys,
)
from app.infra.db import create_engine, create_session_factory
from app.infra.events import EventBroker
from app.infra.message_bus import MessageBus
from app.infra.metrics import metrics_response
from app.infra.monitoring import MonitoringCollector, set_collector
from app.infra.notification_sender import LogNotificationSender
from app.infra.payment_channels import build_payment_channels
from app.infra.payment_reconcile import OrderReconciler, run_order_reconcile_loop
from app.infra.presence_aggregator import MinuteAggregator
from app.infra.redis_state import build_code_limiter, build_lockout, build_redis_client
from app.infra.sampling import run_monitoring_loop
from app.infra.scheduler_loop import run_scheduler_loop


def _default_introspector(
    config: Settings, token_manager: Ed25519TokenManager
) -> TokenIntrospector:
    """按环境选择默认鉴权器：
    - dev：JWT 验签，失败回退演示主体（admin/operator）便于看板联调；
    - test：原始令牌透传（中间件/依赖测试语义）；
    - 其余：严格 JWT 验签（失败即未认证，配合 auth_enforce）。
    """
    if config.env == "dev":
        return FallbackJwtIntrospector(token_manager, ("admin", "operator"))
    if config.env == "test":
        return DevTokenIntrospector()
    return JwtTokenIntrospector(token_manager)


def create_app(
    settings: Settings | None = None,
    *,
    introspector: TokenIntrospector | None = None,
) -> FastAPI:
    """装配应用（依赖注入点：settings 与 introspector 可注入便于测试与 SP2-2 替换）。"""
    config = settings or Settings()
    setup_logging(config.log_level)

    # SP2-2：签名密钥（密钥域，生产缺失即快速失败）+ 令牌管理器
    signing_keys = build_signing_keys(config.auth_signing_keys, config.env)
    token_manager = Ed25519TokenManager(
        signing_keys, config.auth_issuer, config.auth_access_ttl_seconds
    )

    # 采集器先于引擎装配：数据库引擎注册 SQL 耗时钩子时即可回收到样本
    collector = MonitoringCollector()
    set_collector(collector)
    engine = create_engine(config.database_url, echo=config.database_echo)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(run_monitoring_loop(app)),
            # 在线分钟桶聚合后台落库（SP2-8 优化项 1：消除心跳单行漏斗）
            asyncio.create_task(
                app.state.minute_aggregator.run_loop(config.presence_aggregate_interval_seconds)
            ),
        ]
        # 支付查单兜底扫描（EC-N7/DEC-022）：周期为 0 时循环自行退出，兜底只剩轮询与管理端
        if config.order_reconcile_interval_seconds > 0:
            tasks.append(asyncio.create_task(run_order_reconcile_loop(app)))
        # 三大调度任务的自动触发（月赠/到期冻结/对账；关闭时仅剩管理端手动触发）
        if config.scheduler_enabled:
            tasks.append(asyncio.create_task(run_scheduler_loop(app)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            # 最后落库一次（聚合窗口内未写桶不丢）
            with contextlib.suppress(Exception):
                await app.state.minute_aggregator.flush_and_prune()
            await engine.dispose()

    app = FastAPI(
        title="Erdos 云端服务端",
        version=__version__,
        description="Erdos 服务端（SP2-1 骨架）：统一信封、网关与数据访问层",
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.engine = engine
    app.state.session_factory = session_factory
    # dev/test/prod 环境策略见 _default_introspector（SP2-2 JWT 验签收口）
    app.state.introspector = introspector or _default_introspector(config, token_manager)
    app.state.event_broker = EventBroker()  # 看板实时推送（进程内，单实例）
    # 在线分钟桶进程内聚合器（SP2-8 优化项 1）：心跳侧 record，后台循环落库
    app.state.minute_aggregator = MinuteAggregator(
        session_factory, config.presence_trend_retention_days
    )
    # SP2-2 认证授权：令牌管理器 / 凭据源 / 防爆破（进程级单例）
    app.state.token_manager = token_manager
    hasher = BcryptPasswordHasher()
    app.state.password_hasher = hasher
    # SP2-4 积分域：阶段许可签名器（复用签名密钥集）
    app.state.license_signer = Ed25519LicenseSigner(signing_keys)
    # SP2-7 通知与调度：消息总线 + 通知发送器（dev 日志渠道）+ 验证码限流器（进程级）
    app.state.message_bus = MessageBus()
    app.state.notification_sender = LogNotificationSender()
    # 业务告警出口（对账差异/资金差异等）：告警事件落库 + 看板 SSE + Webhook 外发，静默窗口去重
    app.state.alert_outlet = BrokerAlertOutlet(
        session_factory,
        app.state.event_broker,
        AlertWebhookDispatcher.from_settings(config),
    )
    # SP2-5 查单兜底（EC-N7/DEC-022）：渠道注册表 + 编排器；mock 渠道仅 dev/test 注册
    app.state.payment_channels = build_payment_channels(config)
    app.state.order_reconciler = OrderReconciler(
        session_factory,
        app.state.payment_channels,
        config,
        app.state.license_signer,
        app.state.alert_outlet,
    )
    # Redis 跨进程状态（SP2-7 多实例迁移）：配置 ERDOS_REDIS_URL 时防爆破/验证码限流
    # 自动切换；未配置或 redis 包缺失回退进程内实现（可用性优先）。
    redis_client = build_redis_client(config.redis_url)
    app.state.redis_client = redis_client
    app.state.code_limiter = build_code_limiter(config, redis_client)
    dev_verifier = DevCredentialVerifier.parse(config.auth_dev_users)
    app.state.credential_verifier = SqlCredentialVerifier(
        session_factory, hasher, fallback=dev_verifier
    )
    app.state.auth_lockout = build_lockout(config, redis_client)
    if config.env not in ("dev", "test") and redis_client is None:
        get_logger("erdos.main").warning(
            "ERDOS_REDIS_URL 未配置：防爆破与验证码限流为进程内实现，多实例部署需配置 Redis（SP2-7）"
        )
    # SP2-3 账号域：密码策略 + 重置服务（令牌限流 + dev 日志渠道）
    app.state.reset_notifier = LogResetNotifier()
    app.state.reset_service = PasswordResetService(
        hasher,
        PasswordPolicy(config.account_password_min_length),
        app.state.reset_notifier,
        token_ttl_seconds=config.account_reset_token_ttl_seconds,
        resend_seconds=config.account_reset_resend_seconds,
        daily_limit=config.account_reset_daily_limit,
    )
    if config.env not in ("dev", "test") and not config.auth_enforce:
        get_logger("erdos.main").warning(
            "auth_enforce=False 与生产环境不匹配：外网部署必须置为 True（SP2-2 红线）"
        )
    app.state.monitoring = collector  # 运行监测采集器（中间件/引擎钩子共享）
    app.state.monitoring_last = None  # 最新采样（overview 缓存；采样器启动前为 None）
    app.state.monitoring_alerts = {}  # 活跃告警（metric -> 文案）
    app.state.started_at = utc_now()  # 进程启动时刻（uptime 口径）

    # 中间件注册顺序即执行顺序（后注册先包裹反转）：RequestId -> Metrics -> 限流 -> 鉴权
    app.add_middleware(
        AuthPassthroughMiddleware,
        introspector=app.state.introspector,
        enforce=config.auth_enforce,
    )
    app.add_middleware(RateLimitMiddleware, rules=build_rate_limits(config))
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestIdMiddleware, header_name=config.request_id_header)

    # 全局异常处理：一切异常收敛为统一信封
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    if config.metrics_enabled:
        app.add_api_route("/metrics", metrics_response, methods=["GET"], include_in_schema=False)

    # 管理看板：静态单页（自包含，消费 /v1/admin/dashboard/* 与 SSE）
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin-dashboard")

    app.include_router(api_router)
    return app


app = create_app()