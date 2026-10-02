"""内容库服务（SP2-6）：按会员层级读取内容 + manifest 增量清单生成。

层级规则（对齐 PRD F-005/内容权益）：
- problems：visibility=public 示例题人人可读；member 需活跃订阅；
- paper_templates：tier=free 免费；member_only 需活跃订阅；
- cases：全部为会员内容，强制携带 compliance_note。
"""

from typing import Protocol

from app.core.clock import utc_now
from app.domain.entitlement.ports import (
    CaseRecord,
    CaseRepository,
    ContentScope,
    ManifestRecord,
    ManifestRepository,
    ProblemRecord,
    ProblemRepository,
    TemplateRecord,
    TemplateRepository,
)


class MembershipProvider(Protocol):
    """会员状态提供方（适配 Billing 域订阅状态）。"""

    async def is_active_member(self, user_id: str) -> bool:
        """用户是否活跃会员（任一订阅在有效期内）。"""
        ...


class ContentService:
    """内容用例：层级读取 + 管理侧 CRUD + manifest 生成。"""

    def __init__(
        self,
        problems: ProblemRepository,
        templates: TemplateRepository,
        cases: CaseRepository,
        manifests: ManifestRepository,
        membership: MembershipProvider,
    ) -> None:
        self._problems = problems
        self._templates = templates
        self._cases = cases
        self._manifests = manifests
        self._membership = membership

    # ------------------------------------------------------------------
    # 读取（按会员层级）
    # ------------------------------------------------------------------
    async def list_problems(self, user_id: str) -> list[ProblemRecord]:
        """真题：会员看全部（public+member），非会员只看 public。"""
        if await self._membership.is_active_member(user_id):
            return await self._problems.list_all()
        return await self._problems.list_public()

    async def list_templates(self, user_id: str) -> list[TemplateRecord]:
        """模板：会员看全部，非会员只看 free。"""
        if await self._membership.is_active_member(user_id):
            return await self._templates.list_all()
        return await self._templates.list_free()

    async def list_cases(self, user_id: str) -> list[CaseRecord]:
        """案例：会员专属；非会员返回空（案例是付费内容）。"""
        if not await self._membership.is_active_member(user_id):
            return []
        return await self._cases.list_all()

    # ------------------------------------------------------------------
    # 管理侧 CRUD
    # ------------------------------------------------------------------
    async def upsert_problem(self, record: ProblemRecord) -> ProblemRecord:
        await self._problems.upsert(record)
        await self._bump_manifest(ContentScope.PROBLEMS.value)
        return record

    async def upsert_template(self, record: TemplateRecord) -> TemplateRecord:
        await self._templates.upsert(record)
        await self._bump_manifest(ContentScope.TEMPLATES.value)
        return record

    async def upsert_case(self, record: CaseRecord) -> CaseRecord:
        # 合规红线：案例必须携带合规声明
        if not record.compliance_note.strip():
            raise ValueError("案例必须携带合规声明 compliance_note")
        await self._cases.upsert(record)
        await self._bump_manifest(ContentScope.CASES.value)
        return record

    # ------------------------------------------------------------------
    # manifest 增量
    # ------------------------------------------------------------------
    async def get_manifest(self, scope: str) -> ManifestRecord | None:
        return await self._manifests.get(scope)

    async def list_manifests(self) -> list[ManifestRecord]:
        """列出全部 scope 的 manifest（客户端启动时拉取比对）。"""
        result = []
        for scope in (ContentScope.PROBLEMS.value, ContentScope.TEMPLATES.value, ContentScope.CASES.value):
            m = await self._manifests.get(scope)
            if m is not None:
                result.append(m)
        return result

    async def _bump_manifest(self, scope: str) -> None:
        """内容变更后重建该 scope 的 manifest（version +1，items 为 business_id+sha256 列表）。"""
        existing = await self._manifests.get(scope)
        version = (existing.version + 1) if existing is not None else 1

        if scope == ContentScope.PROBLEMS.value:
            problems = await self._problems.list_all()
            items = [{"business_id": r.business_id, "year": r.year, "problem_code": r.problem_code} for r in problems]
        elif scope == ContentScope.TEMPLATES.value:
            templates = await self._templates.list_all()
            items = [{"business_id": r.business_id, "sha256": r.sha256, "version": r.version} for r in templates]
        else:
            cases = await self._cases.list_all()
            items = [{"business_id": r.business_id, "sha256": r.sha256} for r in cases]

        await self._manifests.upsert(scope, version, items, utc_now())
