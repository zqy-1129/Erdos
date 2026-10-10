"""阶段7：构建不可变候选 release（catalog/integrity/manifest/entities/assets/templates/retrieval/seed/provenance/reports）。"""
import json

from . import core

OBJ_KEY = "content/cumcm/assets/{asset_id}/v{version}/{sha}/{filename}"


def _asset(asset_id, version, role, competition_id, key, sha, size, media, license=None):
    return {
        "asset_id": asset_id, "version": version, "role": role, "competition_id": competition_id,
        "object_key": key, "sha256": sha, "size_bytes": size, "media_type": media,
        "license": license or {"internal_analysis": False, "distribute_original": False,
                               "distribute_profile": False, "send_to_third_party_model": False},
    }


def main(args):
    run_dir = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id
    rel_dir = core.CONTENT_DIR / "out" / "cumcm_delivery" / "releases" / args.release_id
    for sub in ("entities", "assets", "templates", "retrieval", "seed", "provenance", "reports"):
        (rel_dir / sub).mkdir(parents=True, exist_ok=True)

    # 读资产清单
    sources = core.load_jsonl(run_dir / "sources" / "source_files.jsonl")
    in_scope = [r for r in sources if r["in_scope"]]

    assets = []
    local_bindings = {}
    seen_sha = {}
    for r in in_scope:
        if r["status"] != "ok" or not r["sha256"]:
            continue
        aid = "cumcm-asset-" + r["sha256"][:12]
        ver = 1
        fn = r["relative_path"].split("/")[-1]
        key = OBJ_KEY.format(asset_id=aid, version=ver, sha=r["sha256"], filename=fn)
        if r["sha256"] in seen_sha:
            # 字节相同多来源：资产只建一个，来源路径多条（design：字节按 SHA 去重，来源保留）
            local_bindings[aid]["sources"].append(r["relative_path"])
            continue
        seen_sha[r["sha256"]] = aid
        assets.append(_asset(aid, ver, r["role_hint"], "cumcm", key, r["sha256"], r["size"], r["media_type"]))
        local_bindings.setdefault(aid, {"r": r["relative_path"], "sources": [r["relative_path"]]})

    # 实体（relate 结果）
    problems = core.load_jsonl(run_dir / "problems.jsonl")
    subproblems = core.load_jsonl(run_dir / "subproblems.jsonl")
    papers = core.load_jsonl(run_dir / "papers.jsonl")
    links = core.load_jsonl(run_dir / "paper_problem_links.jsonl")
    recipes_w = core.load_json(run_dir / "recipes" / "writing_recipes.json")
    recipes_f = core.load_json(run_dir / "recipes" / "figure_recipes.json")
    templates = core.load_json(run_dir / "templates" / "dependency_manifest.json")
    ruleset = core.load_json(run_dir / "templates" / "ruleset.json")

    secure = {"problems": problems, "subproblems": subproblems, "papers": papers,
              "paper_problem_links": links, "assets": assets,
              "writing_recipes": recipes_w, "figure_recipes": recipes_f,
              "rulesets": [ruleset]}
    for name, rows in secure.items():
        core.write_json(rel_dir / "entities" / (name + ".json"), rows)

    # bundle v2
    bundle = {
        "release_id": args.release_id, "schema_version": 2,
        "problems": problems, "subproblems": subproblems, "papers": papers,
        "paper_problem_links": links, "assets": assets,
        "writing_recipes": recipes_w, "figure_recipes": recipes_f,
        "rulesets": [ruleset], "is_fixture": False,
    }
    core.write_json(rel_dir / "entities" / "bundle.json", bundle)

    core.write_json(rel_dir / "assets" / "local_bindings.json", local_bindings)
    core.write_json(rel_dir / "retrieval" / "writing_recipes.json", recipes_w)
    core.write_json(rel_dir / "retrieval" / "figure_recipes.json", recipes_f)

    # seed（旧键集合：ProblemRecord/CaseRecord/TemplateRecord）
    seed_problems = [p for p in problems if p.get("subproblems")]
    core.write_json(rel_dir / "seed" / "problems.json", seed_problems)
    core.write_json(rel_dir / "seed" / "cases.json", papers)
    core.write_json(rel_dir / "seed" / "templates.json", [{
        "business_id": "cumcm-latex-family-v1", "competition": "cumcm", "format": "latex",
        "oss_key": "templates/cumcm-latex-family-v1.zip", "sha256": templates["latex_zip"]["sha256"],
        "version": 1, "changelog": "国赛 LaTeX 完整依赖包（候选，未编译，规则待核验）", "tier": "free"}])

    # release_manifest
    manifest = {
        "release_id": args.release_id, "version": 1, "competition_id": "cumcm",
        "schema_version": 2, "published_status": "staging",
        "items": [{"asset_id": a["asset_id"], "version": a["version"], "sha256": a["sha256"],
                   "role": a["role"], "change": "add"} for a in assets],
        "dependencies": [], "removed": [],
    }
    core.write_json(rel_dir / "manifest.json", manifest)

    # catalog
    catalog = {
        "release_id": args.release_id, "version": 1, "published_status": "staging",
        "competition_id": "cumcm", "historical_year_range": [2010, 2025],
        "counts": {
            "source_files_in_scope": len(in_scope),
            "problem_entities": len(problems),
            "problem_entities_with_subproblems": sum(1 for p in problems if p.get("subproblems")),
            "subproblem_entities": len(subproblems),
            "paper_entities": len(papers),
            "confirmed_parent_links": sum(1 for l in links if l["relation_status"] == "confirmed"),
            "unresolved_papers": sum(1 for l in links if l["relation_status"] == "unresolved"),
            "writing_recipes": len(recipes_w), "figure_recipes": len(recipes_f),
            "assets": len(assets),
        },
        "retrieval_mode": "lexical_only",
        "ruleset_status": ruleset["verification_status"],
    }
    core.write_json(rel_dir / "catalog.json", catalog)

    # integrity（全文件 SHA，排除自身；时间戳放 meta，不进业务 hash）
    integrity = {"release_id": args.release_id, "files": {}}
    for p in sorted(rel_dir.rglob("*")):
        if p.is_file() and p.name != "integrity.json":
            integrity["files"][p.relative_to(rel_dir).as_posix()] = core.sha256_of(p)
    core.write_json(rel_dir / "integrity.json", integrity)
    core.write_json(rel_dir / "meta.json", {"generated_at": core.now_utc_iso()})

    core.write_json(rel_dir / "reports" / "release_summary.json", catalog)

    print("pack 完成：release={}，实体题={}/小问={}/论文={}，确认父题={}，资产={}，图/write方案={}/{}".format(
        args.release_id, len(problems), len(subproblems), len(papers),
        catalog["counts"]["confirmed_parent_links"], len(assets), len(recipes_f), len(recipes_w)))
    return 0
