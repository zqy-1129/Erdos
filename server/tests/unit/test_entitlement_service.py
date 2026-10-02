"""权益快照与内容服务测试（SP2-6）：快照签发/验签 + 会员层级读取 + manifest 增量。

覆盖：
- 快照签发（聚合订阅+积分状态）
- 快照验签（篡改任一字段验签失败）
- 会员层级读取（public/free 人人可读，member 需活跃订阅，案例会员专属）
- 案例合规声明强制
- manifest 版本递增与 items 正确
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.entitlement.content_service import ContentService
from app.domain.entitlement.ports import CaseRecord, TemplateRecord
from app.domain.entitlement.service import EntitlementService
from app.domain.points.ports import BalanceType
from app.infra.auth import Ed25519LicenseSigner, build_signing_keys
from app.repository.entitlement import (
    SQLAlchemyCaseRepository,
    SQLAlchemyManifestRepository,
    SQLAlchemyProblemRepository,
    SQLAlchemySnapshotRepository,
    SQLAlchemyTemplateRepository,
)
from app.repository.models import PaperTemplate, Problem
from app.repository.points import SQLAlchemyPointAccountRepository
from app.repository.uow import UnitOfWork


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class FakeSubscriptionProvider:
    """订阅状态桩。"""

    def __init__(self, active: bool, end_at: datetime | None = None) -> None:
        self._active = active
        self._end_at = end_at

    async def active_until(self, user_id: str) -> tuple[bool, datetime | None]:
        return self._active, self._end_at


class FakeMembershipProvider:
    """会员状态桩。"""

    def __init__(self, active: bool) -> None:
        self._active = active

    async def is_active_member(self, user_id: str) -> bool:
        return self._active


def _signer() -> Ed25519LicenseSigner:
    keys = build_signing_keys("test-1:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "test")
    return Ed25519LicenseSigner(keys)


def _entitlement_svc(session, signer, active: bool) -> EntitlementService:
    return EntitlementService(
        SQLAlchemySnapshotRepository(session),
        signer,
        FakeSubscriptionProvider(active, datetime.now(UTC) + timedelta(days=30) if active else None),
        SQLAlchemyPointAccountRepository(session),
    )


def _content_svc(session, active: bool) -> ContentService:
    return ContentService(
        SQLAlchemyProblemRepository(session),
        SQLAlchemyTemplateRepository(session),
        SQLAlchemyCaseRepository(session),
        SQLAlchemyManifestRepository(session),
        FakeMembershipProvider(active),
    )


# ----------------------------------------------------------------------
# 快照签发 / 验签
# ----------------------------------------------------------------------
async def test_snapshot_issue_and_verify(session_factory, settings) -> None:
    """快照签发：聚合订阅+积分状态，签名可验签。"""
    signer = _signer()
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyPointAccountRepository(uow.session).get_or_create(
            "u1", datetime.now(UTC)
        )
        await SQLAlchemyPointAccountRepository(uow.session).credit(
            "u1", BalanceType.PURCHASED, 400, datetime.now(UTC)
        )
        svc = _entitlement_svc(uow.session, signer, active=True)
        result = await svc.issue_snapshot("u1", datetime.now(UTC))

    assert result.payload["subscribed"] is True
    assert result.payload["purchased_balance"] == 400
    assert result.signature  # 有签名
    assert signer.verify(
        _canonical(result.payload), result.signature, result.key_version
    ) is True


async def test_snapshot_tamper_fails_verify(session_factory, settings) -> None:
    """篡改任一字段验签失败（验收标准）。"""
    signer = _signer()
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyPointAccountRepository(uow.session).get_or_create(
            "u1", datetime.now(UTC)
        )
        svc = _entitlement_svc(uow.session, signer, active=False)
        result = await svc.issue_snapshot("u1", datetime.now(UTC))

    # 篡改 purchased_balance
    tampered = dict(result.payload)
    tampered["purchased_balance"] = 99999
    assert signer.verify(_canonical(tampered), result.signature, result.key_version) is False

    # 篡改 frozen
    tampered2 = dict(result.payload)
    tampered2["frozen"] = True
    assert signer.verify(_canonical(tampered2), result.signature, result.key_version) is False

    # 篡改 subscribed
    tampered3 = dict(result.payload)
    tampered3["subscribed"] = True
    assert signer.verify(_canonical(tampered3), result.signature, result.key_version) is False


async def test_snapshot_non_subscriber(session_factory, settings) -> None:
    """非订阅用户快照：subscribed=False。"""
    signer = _signer()
    async with UnitOfWork(session_factory) as uow:
        svc = _entitlement_svc(uow.session, signer, active=False)
        result = await svc.issue_snapshot("u1", datetime.now(UTC))
    assert result.payload["subscribed"] is False
    assert result.payload["sub_end_at"] is None


# ----------------------------------------------------------------------
# 会员层级读取
# ----------------------------------------------------------------------
def _seed_content(session) -> None:
    session.add_all([
        Problem(
            business_id="cumcm-2024-A", competition="cumcm", year=2024, problem_code="A",
            title="示例题", tags=[], prompt_zh="题干", visibility="public",
        ),
        Problem(
            business_id="cumcm-2024-B", competition="cumcm", year=2024, problem_code="B",
            title="会员题", tags=[], prompt_zh="题干", visibility="member",
        ),
        PaperTemplate(
            business_id="tpl-free", competition="cumcm", format="latex",
            oss_key="oss/free.tex", sha256="a" * 64, tier="free",
        ),
        PaperTemplate(
            business_id="tpl-member", competition="cumcm", format="latex",
            oss_key="oss/member.tex", sha256="b" * 64, tier="member_only",
        ),
    ])


async def test_problem_membership_filtering(session_factory, settings) -> None:
    """真题层级：非会员只看 public，会员看全部。"""
    async with UnitOfWork(session_factory) as uow:
        _seed_content(uow.session)
        await uow.session.flush()
        non_member = await _content_svc(uow.session, active=False).list_problems("u1")
        member = await _content_svc(uow.session, active=True).list_problems("u1")
    assert {p.business_id for p in non_member} == {"cumcm-2024-A"}
    assert {p.business_id for p in member} == {"cumcm-2024-A", "cumcm-2024-B"}


async def test_template_tier_filtering(session_factory, settings) -> None:
    """模板层级：非会员只看 free，会员看全部。"""
    async with UnitOfWork(session_factory) as uow:
        _seed_content(uow.session)
        await uow.session.flush()
        non_member = await _content_svc(uow.session, active=False).list_templates("u1")
        member = await _content_svc(uow.session, active=True).list_templates("u1")
    assert {t.business_id for t in non_member} == {"tpl-free"}
    assert {t.business_id for t in member} == {"tpl-free", "tpl-member"}


async def test_cases_member_only(session_factory, settings) -> None:
    """案例：非会员返回空，会员可读。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _content_svc(uow.session, active=False)
        assert await svc.list_cases("u1") == []
        member_svc = _content_svc(uow.session, active=True)
        await member_svc.upsert_case(CaseRecord(
            business_id="case-1", problem_id="cumcm-2024-A", title="案例",
            award="国一", method_tags=[], oss_key="oss/case.pdf",
            sha256="c" * 64, compliance_note="仅作方法参照，禁止大段抄袭",
        ))
        items = await member_svc.list_cases("u1")
        assert len(items) == 1
        assert items[0].compliance_note == "仅作方法参照，禁止大段抄袭"


async def test_case_requires_compliance_note(session_factory, settings) -> None:
    """案例合规声明强制：空 compliance_note 抛错。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _content_svc(uow.session, active=True)
        with pytest.raises(ValueError):
            await svc.upsert_case(CaseRecord(
                business_id="case-2", problem_id="p", title="案例",
                award="", method_tags=[], oss_key="o", sha256="d" * 64, compliance_note="",
            ))


async def test_manifest_incremental(session_factory, settings) -> None:
    """manifest 增量：内容变更后 version +1，items 含 business_id+sha256。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _content_svc(uow.session, active=True)
        await svc.upsert_template(TemplateRecord(
            business_id="tpl-1", competition="cumcm", format="latex",
            oss_key="oss/a.tex", sha256="e" * 64, version=1, changelog="", tier="free",
        ))
        m1 = await svc.get_manifest("templates")
        assert m1 is not None and m1.version == 1
        assert m1.items == [{"business_id": "tpl-1", "sha256": "e" * 64, "version": 1}]

        # 再更新一次，version +1
        await svc.upsert_template(TemplateRecord(
            business_id="tpl-2", competition="cumcm", format="latex",
            oss_key="oss/b.tex", sha256="f" * 64, version=1, changelog="", tier="free",
        ))
        m2 = await svc.get_manifest("templates")
        assert m2 is not None and m2.version == 2
        assert len(m2.items) == 2
