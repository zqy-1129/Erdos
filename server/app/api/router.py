"""API v1 路由聚合（统一 /v1 前缀，对应契约 paths）。"""

from fastapi import APIRouter

from app.api.v1 import (
    account,
    analytics,
    audit,
    auth,
    billing,
    dashboard,
    entitlements,
    health,
    monitoring,
    notifications,
    points,
    presence,
    telemetry,
)

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(account.router)
api_router.include_router(audit.router)
api_router.include_router(dashboard.router)
api_router.include_router(monitoring.router)
api_router.include_router(presence.router)
api_router.include_router(points.router)
api_router.include_router(billing.router)
api_router.include_router(entitlements.router)
api_router.include_router(notifications.router)
api_router.include_router(telemetry.router)
api_router.include_router(analytics.router)