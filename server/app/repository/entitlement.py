"""权益与内容域仓储落库实现（entitlement_snapshots / problems / paper_templates / cases / content_manifests）。

内容附件一律存 OSS，库内只存 oss_key + sha256；
cases.compliance_note 强制非空（领域校验）。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entitlement.ports import (
    CaseRecord,
    CaseRepository,
    EntitlementSnapshotRecord,
    ManifestRecord,
    ManifestRepository,
    ProblemRecord,
    ProblemRepository,
    SnapshotRepository,
    TemplateRecord,
    TemplateRepository,
)
from app.repository.models import (
    Case,
    ContentManifest,
    EntitlementSnapshot,
    PaperTemplate,
    Problem,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_required(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("非空时间列读取为 NULL")
    return _ensure_utc(value)  # type: ignore[return-value]


def _snapshot(row: EntitlementSnapshot) -> EntitlementSnapshotRecord:
    return EntitlementSnapshotRecord(
        id=row.id,
        user_id=row.user_id,
        payload=dict(row.payload),
        signature=row.signature,
        key_version=row.key_version,
        issued_at=_utc_required(row.issued_at),
    )


def _problem(row: Problem) -> ProblemRecord:
    return ProblemRecord(
        business_id=row.business_id,
        competition=row.competition,
        year=int(row.year),
        problem_code=row.problem_code,
        title=row.title,
        tags=list(row.tags),
        prompt_zh=row.prompt_zh,
        prompt_en=row.prompt_en,
        attachments=list(row.attachments),
        scoring=row.scoring,
        dataset_hint=row.dataset_hint,
        visibility=row.visibility,
    )


def _template(row: PaperTemplate) -> TemplateRecord:
    return TemplateRecord(
        business_id=row.business_id,
        competition=row.competition,
        format=row.format,
        oss_key=row.oss_key,
        sha256=row.sha256,
        version=int(row.version),
        changelog=row.changelog,
        tier=row.tier,
    )


def _case(row: Case) -> CaseRecord:
    return CaseRecord(
        business_id=row.business_id,
        problem_id=row.problem_id,
        title=row.title,
        award=row.award,
        method_tags=list(row.method_tags),
        oss_key=row.oss_key,
        sha256=row.sha256,
        compliance_note=row.compliance_note,
    )


def _manifest(row: ContentManifest) -> ManifestRecord:
    return ManifestRecord(
        scope=row.scope,
        version=int(row.version),
        items=list(row.items),
        updated_at=_utc_required(row.updated_at),
    )


class SQLAlchemySnapshotRepository(SnapshotRepository):
    """entitlement_snapshots 表实现（追加式）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, record: EntitlementSnapshotRecord) -> EntitlementSnapshotRecord:
        row = EntitlementSnapshot(
            id=_new_id() if not record.id else record.id,
            user_id=record.user_id,
            payload=record.payload,
            signature=record.signature,
            key_version=record.key_version,
            issued_at=record.issued_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _snapshot(row)


class SQLAlchemyProblemRepository(ProblemRepository):
    """problems 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, business_id: str) -> ProblemRecord | None:
        row = (
            await self._session.execute(
                select(Problem).where(Problem.business_id == business_id)
            )
        ).scalar_one_or_none()
        return _problem(row) if row is not None else None

    async def list_public(self) -> list[ProblemRecord]:
        rows = (
            (
                await self._session.execute(
                    select(Problem)
                    .where(Problem.visibility == "public")
                    .order_by(Problem.year.desc(), Problem.problem_code)
                )
            )
            .scalars()
            .all()
        )
        return [_problem(r) for r in rows]

    async def list_all(self) -> list[ProblemRecord]:
        rows = (
            (
                await self._session.execute(
                    select(Problem).order_by(Problem.year.desc(), Problem.problem_code)
                )
            )
            .scalars()
            .all()
        )
        return [_problem(r) for r in rows]

    async def upsert(self, record: ProblemRecord) -> ProblemRecord:
        row = Problem(
            business_id=record.business_id,
            competition=record.competition,
            year=record.year,
            problem_code=record.problem_code,
            title=record.title,
            tags=record.tags,
            prompt_zh=record.prompt_zh,
            prompt_en=record.prompt_en,
            attachments=record.attachments,
            scoring=record.scoring,
            dataset_hint=record.dataset_hint,
            visibility=record.visibility,
        )
        await self._session.merge(row)
        await self._session.flush()
        return record


class SQLAlchemyTemplateRepository(TemplateRepository):
    """paper_templates 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, business_id: str) -> TemplateRecord | None:
        row = (
            await self._session.execute(
                select(PaperTemplate).where(PaperTemplate.business_id == business_id)
            )
        ).scalar_one_or_none()
        return _template(row) if row is not None else None

    async def list_free(self) -> list[TemplateRecord]:
        rows = (
            (
                await self._session.execute(
                    select(PaperTemplate)
                    .where(PaperTemplate.tier == "free")
                    .order_by(PaperTemplate.version.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_template(r) for r in rows]

    async def list_all(self) -> list[TemplateRecord]:
        rows = (
            (
                await self._session.execute(
                    select(PaperTemplate).order_by(PaperTemplate.version.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_template(r) for r in rows]

    async def upsert(self, record: TemplateRecord) -> TemplateRecord:
        row = PaperTemplate(
            business_id=record.business_id,
            competition=record.competition,
            format=record.format,
            oss_key=record.oss_key,
            sha256=record.sha256,
            version=record.version,
            changelog=record.changelog,
            tier=record.tier,
        )
        await self._session.merge(row)
        await self._session.flush()
        return record


class SQLAlchemyCaseRepository(CaseRepository):
    """cases 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, business_id: str) -> CaseRecord | None:
        row = (
            await self._session.execute(
                select(Case).where(Case.business_id == business_id)
            )
        ).scalar_one_or_none()
        return _case(row) if row is not None else None

    async def list_all(self) -> list[CaseRecord]:
        rows = (
            (
                await self._session.execute(
                    select(Case).order_by(Case.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_case(r) for r in rows]

    async def upsert(self, record: CaseRecord) -> CaseRecord:
        row = Case(
            business_id=record.business_id,
            problem_id=record.problem_id,
            title=record.title,
            award=record.award,
            method_tags=record.method_tags,
            oss_key=record.oss_key,
            sha256=record.sha256,
            compliance_note=record.compliance_note,
        )
        await self._session.merge(row)
        await self._session.flush()
        return record


class SQLAlchemyManifestRepository(ManifestRepository):
    """content_manifests 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, scope: str) -> ManifestRecord | None:
        row = (
            await self._session.execute(
                select(ContentManifest).where(ContentManifest.scope == scope)
            )
        ).scalar_one_or_none()
        return _manifest(row) if row is not None else None

    async def upsert(
        self, scope: str, version: int, items: list[dict[str, Any]], now: datetime
    ) -> ManifestRecord:
        existing = await self.get(scope)
        if existing is not None:
            row = (
                await self._session.execute(
                    select(ContentManifest).where(ContentManifest.scope == scope)
                )
            ).scalar_one()
            row.version = version
            row.items = items
            row.updated_at = now
        else:
            row = ContentManifest(scope=scope, version=version, items=items, updated_at=now)
            self._session.add(row)
        await self._session.flush()
        return ManifestRecord(
            scope=scope, version=version, items=items, updated_at=now
        )
