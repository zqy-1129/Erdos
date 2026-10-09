"""阶段10：只读 QC。不生成/修复文件、不跑写库测试；缺文件即报，不自动补。"""
import asyncio
import json

from . import core


def _recheck_integrity(release_id):
    rel = core.CONTENT_DIR / "out" / "cumcm_delivery" / "releases" / release_id
    saved = core.load_json(rel / "integrity.json")["files"]
    issues = []
    for relp, want in saved.items():
        p = rel / relp
        if not p.is_file():
            issues.append("missing: " + relp)
            continue
        if core.sha256_of(p) != want:
            issues.append("hash mismatch: " + relp)
    return issues


def _recheck_baseline():
    base = core.load_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "protected_baseline.json")
    issues = []
    for rel, rec in base["files"].items():
        p = core.CONTENT_DIR / rel
        if not p.is_file():
            issues.append("protected missing: " + rel)
        elif core.sha256_of(p) != rec["sha256"]:
            issues.append("protected changed: " + rel)
    return issues


async def _pg_counts():
    from . import importdb
    import asyncpg
    cfg = importdb.pg_cfg(None)
    try:
        conn = await asyncpg.connect(**cfg)
    except Exception as exc:
        return {"status": "unreachable", "reason": str(exc)}
    try:
        rows = await conn.fetch("""SELECT 'problems' t, count(*) FROM content_de.problems
            UNION ALL SELECT 'subproblems', count(*) FROM content_de.subproblems
            UNION ALL SELECT 'papers', count(*) FROM content_de.papers
            UNION ALL SELECT 'links', count(*) FROM content_de.paper_problem_links
            UNION ALL SELECT 'assets', count(*) FROM content_de.assets""")
    finally:
        await conn.close()
    return {"status": "ok", "counts": {r["t"]: r["count"] for r in rows}}


def main(args):
    report = {
        "check": "read_only",
        "release_integrity": _recheck_integrity(args.release_id),
        "protected_baseline": _recheck_baseline(),
        "pg": asyncio.run(_pg_counts()),
    }
    core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "integration" / args.run_id / "read_only_check.json", report)
    ok = not report["release_integrity"] and not report["protected_baseline"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("check 完成：release_integrity={}，protected={}，PG={}".format(
        len(report["release_integrity"]), len(report["protected_baseline"]), report["pg"]["status"]))
    return 0 if ok else 1
