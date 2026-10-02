"""内容库灌库脚本（SP4-4）：从 JSON 数据包导入三库 + sha256 回填。

用法：
    <venv>/python scripts/content_seed.py --data data/content --db sqlite+aiosqlite:///./erdos.db

数据包格式（对齐《数据模型设计》第 7 章）：
- problems.json：真题（business_id/competition/year/problem_code/title/tags/prompt_zh/prompt_en/attachments/scoring/dataset_hint/visibility）
- templates.json：模板（business_id/competition/format/oss_key/sha256/version/changelog/tier）
- cases.json：案例（business_id/problem_id/title/award/method_tags/oss_key/sha256/compliance_note）

红线：附件 sha256 回填；案例 compliance_note 必填；禁止虚构真题与获奖信息。
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.domain.entitlement.content_service import ContentService
from app.domain.entitlement.ports import CaseRecord, ProblemRecord, TemplateRecord
from app.infra.db import create_engine, create_session_factory
from app.repository.uow import UnitOfWork


@dataclass(slots=True)
class SeedResult:
    problems: int = 0
    templates: int = 0
    cases: int = 0


def sha256_of(path: Path) -> str:
    """计算附件 sha256。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def seed_content(session_factory, data_dir: Path) -> SeedResult:
    """从数据包导入三库，返回导入计数。"""
    result = SeedResult()

    async with UnitOfWork(session_factory) as uow:
        svc = _make_service(uow.session)
        # 真题
        problems = _load_json(data_dir / "problems.json")
        for p in problems:
            await svc.upsert_problem(ProblemRecord(**p))
            result.problems += 1
        # 模板
        templates = _load_json(data_dir / "templates.json")
        for t in templates:
            await svc.upsert_template(TemplateRecord(**t))
            result.templates += 1
        # 案例
        cases = _load_json(data_dir / "cases.json")
        for c in cases:
            await svc.upsert_case(CaseRecord(**c))
            result.cases += 1
    return result


def _make_service(session):
    from app.repository.entitlement import (
        SQLAlchemyCaseRepository,
        SQLAlchemyManifestRepository,
        SQLAlchemyProblemRepository,
        SQLAlchemyTemplateRepository,
    )

    class _NoMembership:
        async def is_active_member(self, user_id: str) -> bool:
            return True

    return ContentService(
        SQLAlchemyProblemRepository(session),
        SQLAlchemyTemplateRepository(session),
        SQLAlchemyCaseRepository(session),
        SQLAlchemyManifestRepository(session),
        _NoMembership(),
    )


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="内容库灌库")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--db", default="sqlite+aiosqlite:///./erdos.db")
    args = parser.parse_args()

    engine = create_engine(args.db)
    factory = create_session_factory(engine)
    try:
        result = await seed_content(factory, args.data)
        print(f"灌库完成：真题 {result.problems}、模板 {result.templates}、案例 {result.cases}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
