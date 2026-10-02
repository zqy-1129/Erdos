"""权益与内容接口（SP2-6）：快照签发 + 内容读取（会员层级）+ 管理侧 CRUD + manifest。

资金/内容红线：快照 Ed25519 签名（客户端离线验签）；内容附件只存 oss_key+sha256；
案例强制携带合规声明；管理侧 CRUD 需 admin 角色。
"""

from dataclasses import asdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, require_principal, require_roles
from app.core.clock import utc_now
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.entitlement.content_service import ContentService
from app.domain.entitlement.ports import (
    CaseRecord,
    ProblemRecord,
    TemplateRecord,
)
from app.domain.entitlement.service import EntitlementService
from app.infra.auth import Principal
from app.repository.billing import SQLAlchemySubscriptionRepository
from app.repository.entitlement import (
    SQLAlchemyCaseRepository,
    SQLAlchemyManifestRepository,
    SQLAlchemyProblemRepository,
    SQLAlchemySnapshotRepository,
    SQLAlchemyTemplateRepository,
)
from app.repository.points import SQLAlchemyPointAccountRepository
from app.repository.uow import UnitOfWork

router = APIRouter(tags=["entitlements"])


# ----------------------------------------------------------------------
# 视图模型
# ----------------------------------------------------------------------
class SnapshotView(BaseModel):
    payload: dict
    signature: str
    key_version: str
    issued_at: datetime


class ProblemView(BaseModel):
    business_id: str
    competition: str
    year: int
    problem_code: str
    title: str
    tags: list[str]
    prompt_zh: str
    prompt_en: str
    attachments: list[dict]
    scoring: str
    dataset_hint: str
    visibility: str


class TemplateView(BaseModel):
    business_id: str
    competition: str
    format: str
    oss_key: str
    sha256: str
    version: int
    changelog: str
    tier: str


class CaseView(BaseModel):
    business_id: str
    problem_id: str
    title: str
    award: str
    method_tags: list[str]
    oss_key: str
    sha256: str
    compliance_note: str


class ManifestView(BaseModel):
    scope: str
    version: int
    items: list[dict]
    updated_at: datetime


class ProblemBody(BaseModel):
    business_id: str = Field(min_length=1, max_length=64)
    competition: str = Field(min_length=1, max_length=16)
    year: int
    problem_code: str = Field(min_length=1, max_length=8)
    title: str = Field(min_length=1, max_length=128)
    tags: list[str] = []
    prompt_zh: str = Field(min_length=1, max_length=8192)
    prompt_en: str = ""
    attachments: list[dict] = []
    scoring: str = ""
    dataset_hint: str = ""
    visibility: str = Field(default="public", pattern="^(public|member)$")


class TemplateBody(BaseModel):
    business_id: str = Field(min_length=1, max_length=64)
    competition: str = Field(min_length=1, max_length=16)
    format: str = Field(min_length=1, max_length=16)
    oss_key: str = Field(min_length=1, max_length=256)
    sha256: str = Field(min_length=1, max_length=64)
    version: int = 1
    changelog: str = ""
    tier: str = Field(default="member_only", pattern="^(member_only|free)$")


class CaseBody(BaseModel):
    business_id: str = Field(min_length=1, max_length=64)
    problem_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=128)
    award: str = ""
    method_tags: list[str] = []
    oss_key: str = Field(min_length=1, max_length=256)
    sha256: str = Field(min_length=1, max_length=64)
    compliance_note: str = Field(default="仅作方法参照，禁止大段抄袭", min_length=1, max_length=256)


# ----------------------------------------------------------------------
# 依赖装配
# ----------------------------------------------------------------------
class _SubscriptionStateProvider:
    """订阅状态提供方：任一计划活跃且在有效期内即会员。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_until(self, user_id: str) -> tuple[bool, datetime | None]:
        repo = SQLAlchemySubscriptionRepository(self._session)
        latest_end = None
        for plan in ("monthly", "yearly"):
            sub = await repo.get(user_id, plan)
            if sub is not None and sub.status == "active" and sub.end_at > utc_now():
                if latest_end is None or sub.end_at > latest_end:
                    latest_end = sub.end_at
        return (latest_end is not None, latest_end)


class _MembershipProvider:
    """会员提供方：复用订阅状态提供方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_active_member(self, user_id: str) -> bool:
        provider = _SubscriptionStateProvider(self._session)
        active, _ = await provider.active_until(user_id)
        return active


def _entitlement_service(request: Request, session: AsyncSession) -> EntitlementService:
    return EntitlementService(
        SQLAlchemySnapshotRepository(session),
        request.app.state.license_signer,
        _SubscriptionStateProvider(session),
        SQLAlchemyPointAccountRepository(session),
    )


def _content_service(session: AsyncSession) -> ContentService:
    return ContentService(
        SQLAlchemyProblemRepository(session),
        SQLAlchemyTemplateRepository(session),
        SQLAlchemyCaseRepository(session),
        SQLAlchemyManifestRepository(session),
        _MembershipProvider(session),
    )


# ----------------------------------------------------------------------
# 权益快照
# ----------------------------------------------------------------------
@router.get("/entitlements/snapshot", response_model=Envelope[SnapshotView], summary="权益快照签发")
async def snapshot(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[SnapshotView]:
    """聚合订阅+积分状态，签发 Ed25519 签名快照（客户端离线验签）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _entitlement_service(request, uow.session).issue_snapshot(
            principal.subject, now
        )
        view = SnapshotView(
            payload=result.payload,
            signature=result.signature,
            key_version=result.key_version,
            issued_at=result.issued_at,
        )
    return ok(view, request_id_var.get())


# ----------------------------------------------------------------------
# 内容读取（会员层级）
# ----------------------------------------------------------------------
@router.get("/content/problems", response_model=Envelope[list[ProblemView]], summary="真题列表")
async def list_problems(
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[list[ProblemView]]:
    async with UnitOfWork(session_factory) as uow:
        items = await _content_service(uow.session).list_problems(principal.subject)
        view = [ProblemView(**asdict(p)) for p in items]
    return ok(view, request_id_var.get())


@router.get("/content/templates", response_model=Envelope[list[TemplateView]], summary="模板列表")
async def list_templates(
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[list[TemplateView]]:
    async with UnitOfWork(session_factory) as uow:
        items = await _content_service(uow.session).list_templates(principal.subject)
        view = [TemplateView(**asdict(t)) for t in items]
    return ok(view, request_id_var.get())


@router.get("/content/cases", response_model=Envelope[list[CaseView]], summary="案例列表")
async def list_cases(
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[list[CaseView]]:
    async with UnitOfWork(session_factory) as uow:
        items = await _content_service(uow.session).list_cases(principal.subject)
        view = [CaseView(**asdict(c)) for c in items]
    return ok(view, request_id_var.get())


@router.get("/content/manifest", response_model=Envelope[list[ManifestView]], summary="增量清单")
async def list_manifests(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[list[ManifestView]]:
    async with UnitOfWork(session_factory) as uow:
        items = await _content_service(uow.session).list_manifests()
        view = [ManifestView(scope=m.scope, version=m.version, items=m.items, updated_at=m.updated_at) for m in items]
    return ok(view, request_id_var.get())


# ----------------------------------------------------------------------
# 管理侧 CRUD（admin）
# ----------------------------------------------------------------------
admin_dep = Annotated[Principal, Depends(require_roles("admin"))]


@router.post("/content/problems", response_model=Envelope[ProblemView], summary="新建/更新真题（admin）")
async def upsert_problem(
    payload: ProblemBody,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[ProblemView]:
    record = ProblemRecord(**payload.model_dump())
    async with UnitOfWork(session_factory) as uow:
        saved = await _content_service(uow.session).upsert_problem(record)
        view = ProblemView(**asdict(saved))
    return ok(view, request_id_var.get())


@router.post("/content/templates", response_model=Envelope[TemplateView], summary="新建/更新模板（admin）")
async def upsert_template(
    payload: TemplateBody,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[TemplateView]:
    record = TemplateRecord(**payload.model_dump())
    async with UnitOfWork(session_factory) as uow:
        saved = await _content_service(uow.session).upsert_template(record)
        view = TemplateView(**asdict(saved))
    return ok(view, request_id_var.get())


@router.post("/content/cases", response_model=Envelope[CaseView], summary="新建/更新案例（admin）")
async def upsert_case(
    payload: CaseBody,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[CaseView]:
    record = CaseRecord(**payload.model_dump())
    async with UnitOfWork(session_factory) as uow:
        saved = await _content_service(uow.session).upsert_case(record)
        view = CaseView(**asdict(saved))
    return ok(view, request_id_var.get())
