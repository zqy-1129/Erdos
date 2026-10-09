"""阶段7：PostgreSQL 导入（dry-run/apply）+ 约束/幂等验证。asyncpg 连接本地 docker PG。"""
import asyncio
import json

from . import core


def pg_cfg(args):
    env = core.try_load_env()
    return {
        "host": env.get("ERDOS_PG_HOST", "127.0.0.1"),
        "port": int(env.get("ERDOS_PG_PORT", "55432")),
        "database": env.get("ERDOS_PG_DATABASE", "cumcm_delivery"),
        "user": env.get("ERDOS_PG_USER", "erdos"),
        "password": env.get("ERDOS_PG_PASSWORD", "erdos_test_pw"),
    }


def _load_release(args):
    rel = core.CONTENT_DIR / "out" / "cumcm_delivery" / "releases" / args.release_id
    return {
        "problems": core.load_json(rel / "entities" / "problems.json"),
        "subproblems": core.load_json(rel / "entities" / "subproblems.json"),
        "papers": core.load_json(rel / "entities" / "papers.json"),
        "links": core.load_json(rel / "entities" / "paper_problem_links.json"),
        "assets": core.load_json(rel / "entities" / "assets.json"),
        "rulesets": core.load_json(rel / "entities" / "rulesets.json"),
    }


def _dry_run(data):
    """离线引用闭合/长度/对象键校验，无写入。返回 issues 列表。"""
    issues = []
    pids = {p["problem_id"] for p in data["problems"]}
    cids = {p["case_id"] for p in data["papers"]}
    for s in data["subproblems"]:
        if s["problem_id"] not in pids:
            issues.append("subproblem {} parent missing: {}".format(s["subproblem_id"], s["problem_id"]))
    for l in data["links"]:
        if l["case_id"] not in cids:
            issues.append("link case missing: " + l["case_id"])
        if l["relation_status"] == "confirmed" and l["problem_id"] not in pids:
            issues.append("confirmed link problem missing: " + l["problem_id"])
    seen = set()
    for a in data["assets"]:
        k = (a["asset_id"], a["version"], a["sha256"])
        if k in seen:
            issues.append("duplicate asset: " + str(k))
        seen.add(k)
        if not ("content/cumcm/assets/" in a["object_key"]):
            issues.append("bad object key: " + a["object_key"])
    return issues


async def _apply(data, cfg):
    import asyncpg
    conn = await asyncpg.connect(**cfg)
    try:
        async with conn.transaction():
            for p in data["problems"]:
                await conn.execute(
                    "INSERT INTO content_de.problems (problem_id, competition_id, code, title, identity_status, review_status, payload)"
                    " VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (problem_id) DO NOTHING",
                    p["problem_id"], p.get("competition_id", "cumcm"),
                    None, None, "pending_review", "pending_review", json.dumps(p, ensure_ascii=False))
            for s in data["subproblems"]:
                await conn.execute(
                    "INSERT INTO content_de.subproblems (subproblem_id, problem_id, requirement, deliverables, constraints, dependencies, schema_version)"
                    " VALUES ($1,$2,$3,'[]'::jsonb,'[]'::jsonb,'[]'::jsonb,2) ON CONFLICT (subproblem_id) DO NOTHING",
                    s["subproblem_id"], s["problem_id"], s.get("requirement"))
            for c in data["papers"]:
                await conn.execute(
                    "INSERT INTO content_de.papers (case_id, problem_id, title, resolution_status, identity_status, version)"
                    " VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (case_id) DO NOTHING",
                    c["case_id"], c.get("problem_id"), None,
                    c.get("resolution_status", "unresolved"), "pending_review", c.get("profile_version", 1))
            for l in data["links"]:
                if l["relation_status"] == "confirmed":
                    await conn.execute(
                        "INSERT INTO content_de.paper_problem_links (link_id, case_id, problem_id, relation_status, evidence)"
                        " VALUES ($1,$2,$3,'confirmed','{}'::jsonb) ON CONFLICT (link_id) DO NOTHING",
                        "link-" + l["case_id"], l["case_id"], l["problem_id"])
            for a in data["assets"]:
                await conn.execute(
                    "INSERT INTO content_de.assets (asset_id, version, role, competition_id, object_key, sha256, size_bytes, media_type, license)"
                    " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (asset_id, version) DO NOTHING",
                    a["asset_id"], a["version"], a["role"], a["competition_id"], a["object_key"],
                    a["sha256"], a["size_bytes"], a["media_type"], json.dumps(a["license"]))
    finally:
        await conn.close()


def main(args):
    data = _load_release(args)
    if args.dry_run and not args.apply:
        issues = _dry_run(data)
        core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "integration" / args.run_id / "import_dry_run.json", {
            "issues": issues, "counts": {"problems": len(data["problems"]), "subproblems": len(data["subproblems"]),
                                         "papers": len(data["papers"]), "links": len(data["links"]), "assets": len(data["assets"])}})
        print("import dry-run 完成：实体 题={}/小问={}/论文={}/资产={}，引用问题={}".format(
            len(data["problems"]), len(data["subproblems"]), len(data["papers"]), len(data["assets"]), len(issues)))
        return 1 if any("missing" in i or "duplicate" in i for i in issues) else 0
    if args.apply:
        cfg = pg_cfg(args)
        asyncio.run(_apply(data, cfg))
        core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "integration" / args.run_id / "import_apply_log.json", {
            "target": args.target, "status": "applied",
            "counts": {"problems": len(data["problems"]), "subproblems": len(data["subproblems"]),
                       "papers": len(data["papers"]), "assets": len(data["assets"])}})
        print("import apply 完成：target={} 已写入 content_de schema。".format(args.target))
        return 0
    return 0
