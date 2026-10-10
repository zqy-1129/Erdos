"""阶段8：bootstrap/retrieve/fetch 适配器（lexical + 门禁；无 embedding 则 lexical_only，不假 vector）。"""
import json

from . import core

STAGES = ["analysis", "assumptions", "symbols", "model", "algorithm", "results", "validation", "sensitivity", "discussion", "abstract"]


def load_release(release_id):
    rel = core.CONTENT_DIR / "out" / "cumcm_delivery" / "releases" / release_id
    return rel


def bootstrap(release_id):
    rel = load_release(release_id)
    cat = core.load_json(rel / "catalog.json")
    ruleset = core.load_json(rel / "entities" / "rulesets.json")[0]
    dep = core.load_json(core.CONTENT_DIR / "normalized" / "cumcm_delivery" / "cumcm-2010-2025-r001" / "templates" / "dependency_manifest.json")
    return {
        "competition_id": "cumcm",
        "release_id": release_id,
        "family_version": 1,
        "ruleset_id": ruleset["ruleset_id"],
        "ruleset_status": ruleset["verification_status"],
        "formats": [{"format": "latex", "package": "cumcm-latex-family-v1.zip", "compiled": False},
                    {"format": "typst", "package": None, "compiled": False}],
        "retrieval_mode": "lexical_only",
        "components": cat["counts"],
    }


def _match_recipes(recipes, req):
    req_pt = set(req["subproblem"].get("problem_types", []))
    stage = req.get("stage")
    out = []
    for rcp in recipes:
        if stage and rcp.get("stage") and rcp["stage"] != stage:
            continue
        if req_pt and rcp.get("problem_types") and not (set(rcp["problem_types"]) - {"unknown"}) & req_pt:
            continue
        out.append(rcp)
    return out


def retrieve(args):
    rel = load_release(args.release_id)
    req = core.load_json(args.request)

    stage = req.get("stage")
    if stage not in STAGES:
        resp = {"request_id": req.get("request_id"), "result": "invalid_request",
                "error_reason": "unsupported stage: " + str(stage)}
        return resp

    if req.get("usage_purpose") == "third_party_model":
        resp = {"request_id": req.get("request_id"), "result": "blocked_usage",
                "error_reason": "历史论文 license.send_to_third_party_model=false，禁止外发",
                "retrieval_mode": "lexical_only"}
        return resp

    wr = core.load_json(rel / "retrieval" / "writing_recipes.json")
    fr = core.load_json(rel / "retrieval" / "figure_recipes.json")

    resp = {
        "request_id": req.get("request_id"),
        "result": "ok",
        "release_id": args.release_id,
        "retrieval_mode": "lexical_only",
        "embedding_profile_id": None,
        "stage": stage,
        "query_summary": json.dumps(req.get("subproblem", {}).get("goal", ""), ensure_ascii=False),
        "units": [],
        "writing_recipes": _match_recipes(wr, req),
        "figure_recipes": _match_recipes(fr, req),
        "missing": [{"kind": "historical_reference_units",
                     "reason": "历史论文许可未授权（internal_analysis=false），正常消费者返回 no_reference，仅提供自研通用 recipe"}],
        "budget_actual": req.get("token_budget"),
        "resource_locks": [],
    }
    if not resp["units"]:
        resp["result"] = "no_reference"
    return resp


def main(args):
    if args.request:
        resp = retrieve(args)
        out = args.output
        if out:
            core.write_json(out, resp)
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 0 if resp["result"] in ("ok", "no_reference") else 1
    # 无 request → bootstrap
    print(json.dumps(bootstrap(args.release_id), ensure_ascii=False, indent=2))
    return 0
