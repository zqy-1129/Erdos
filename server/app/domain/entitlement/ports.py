"""权益与内容域端口与数据载体（SP2-6，与《数据模型设计》权益快照域 + 内容库域对齐）。

关键约束：
- 快照 payload 聚合订阅+积分状态，Ed25519 签名（客户端离线验签）；
- 内容附件一律存 OSS，库内只存 oss_key + sha256；
- 案例条目强制携带 compliance_note（仅作方法参照，禁止大段抄袭）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class ContentScope(StrEnum):
    """manifest 分版本的作用域。"""

    PROBLEMS = "problems"
    TEMPLATES = "templates"
    CASES = "cases"


@dataclass(frozen=True, slots=True)
class EntitlementSnapshotRecord:
    """权益快照视图。"""

    id: str
    user_id: str
    payload: dict[str, Any]
    signature: str
    key_version: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class ProblemRecord:
    """真题视图。"""

    business_id: str
    competition: str
    year: int
    problem_code: str
    title: str
    tags: list[str]
    prompt_zh: str
    prompt_en: str
    attachments: list[dict[str, Any]]
    scoring: str
    dataset_hint: str
    visibility: str


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    """模板视图。"""

    business_id: str
    competition: str
    format: str
    oss_key: str
    sha256: str
    version: int
    changelog: str
    tier: str


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """案例视图。"""

    business_id: str
    problem_id: str
    title: str
    award: str
    method_tags: list[str]
    oss_key: str
    sha256: str
    compliance_note: str


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    """manifest 视图。"""

    scope: str
    version: int
    items: list[dict[str, Any]]
    updated_at: datetime


class SnapshotRepository(Protocol):
    """权益快照仓储端口。"""

    async def append(self, record: EntitlementSnapshotRecord) -> EntitlementSnapshotRecord:
        """落库一份快照（追加式，供审计与回放）。"""
        ...


class ProblemRepository(Protocol):
    """真题仓储端口。"""

    async def get(self, business_id: str) -> ProblemRecord | None:
        ...

    async def list_public(self) -> list[ProblemRecord]:
        """列出 public 真题（示例题，无需会员）。"""
        ...

    async def list_all(self) -> list[ProblemRecord]:
        """列出全部真题（含 member，管理侧用）。"""
        ...

    async def upsert(self, record: ProblemRecord) -> ProblemRecord:
        ...


class TemplateRepository(Protocol):
    """模板仓储端口。"""

    async def get(self, business_id: str) -> TemplateRecord | None:
        ...

    async def list_free(self) -> list[TemplateRecord]:
        ...

    async def list_all(self) -> list[TemplateRecord]:
        ...

    async def upsert(self, record: TemplateRecord) -> TemplateRecord:
        ...


class CaseRepository(Protocol):
    """案例仓储端口。"""

    async def get(self, business_id: str) -> CaseRecord | None:
        ...

    async def list_all(self) -> list[CaseRecord]:
        ...

    async def upsert(self, record: CaseRecord) -> CaseRecord:
        ...


class ManifestRepository(Protocol):
    """manifest 仓储端口。"""

    async def get(self, scope: str) -> ManifestRecord | None:
        ...

    async def upsert(self, scope: str, version: int, items: list[dict[str, Any]], now: datetime) -> ManifestRecord:
        ...


@dataclass(frozen=True, slots=True)
class SnapshotPayload:
    """快照 payload（对齐数据模型 entitlement_snapshots.payload；issued_at 纳入签名防回拨）。"""

    subscribed: bool
    sub_end_at: str | None
    purchased_balance: int
    monthly_balance: int
    frozen: bool
    issued_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "subscribed": self.subscribed,
            "sub_end_at": self.sub_end_at,
            "purchased_balance": self.purchased_balance,
            "monthly_balance": self.monthly_balance,
            "frozen": self.frozen,
            "issued_at": self.issued_at,
        }


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """快照签发结果。"""

    payload: dict[str, Any]
    signature: str
    key_version: str
    issued_at: datetime
    snapshot_id: str = field(default="")
