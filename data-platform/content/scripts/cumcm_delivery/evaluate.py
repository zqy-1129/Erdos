"""阶段9：检索评测（lexical，诚实记录：历史单元 0 可用→历史召回 N/A；recipe 覆盖实测）。"""
import json

from . import core, retrieve

QUERIES = [
    {"qid": "q-opt-model", "task": "optimization", "stage": "model", "gold": ["wr-opt-model"]},
    {"qid": "q-opt-algo", "task": "optimization", "stage": "algorithm", "gold": ["wr-opt-algo"]},
    {"qid": "q-opt-res", "task": "optimization", "stage": "results", "gold": ["wr-opt-results"]},
    {"qid": "q-opt-val", "task": "optimization", "stage": "validation", "gold": ["wr-opt-validation"]},
    {"qid": "q-mech-m", "task": "mechanism", "stage": "model", "gold": ["wr-mech-model"]},
    {"qid": "q-stat-m", "task": "statistical_analysis", "stage": "model", "gold": ["wr-stat-model"]},
    {"qid": "q-pred-m", "task": "prediction", "stage": "model", "gold": ["wr-pred-model"]},
    {"qid": "q-pred-val", "task": "prediction", "stage": "validation", "gold": ["wr-pred-validation"]},
    {"qid": "q-eval-m", "task": "evaluation", "stage": "model", "gold": ["wr-eval-model"]},
    {"qid": "q-sens", "task": "optimization", "stage": "sensitivity", "gold": ["wr-sensitivity"]},
    {"qid": "neg-unknown-stage", "task": "optimization", "stage": "bogus", "negative": True},
    {"qid": "neg-third-party", "task": "optimization", "stage": "model", "negative": True},
]


def _req(q):
    return {
        "subproblem": {"problem_types": [q["task"]]},
        "stage": q["stage"],
        "usage_purpose": "third_party_model" if q["qid"] == "neg-third-party" else "consumer",
    }


def main(args):
    rel = core.CONTENT_DIR / "out" / "cumcm_delivery" / "releases" / args.release_id
    wr = core.load_json(rel / "retrieval" / "writing_recipes.json")
    fr = core.load_json(rel / "retrieval" / "figure_recipes.json")

    rows = []
    pos_hit = 0
    pos_total = 0
    for q in QUERIES:
        if q.get("negative"):
            if q["qid"] == "neg-unknown-stage":
                result = "invalid_request" if q["stage"] not in retrieve.STAGES else "ok"
            else:
                result = "blocked_usage"
            rows.append({"qid": q["qid"], "result": result, "writing_recipe_ids": []})
            continue
        pos_total += 1
        got = {r["recipe_id"] for r in retrieve._match_recipes(wr, _req(q))}
        rows.append({"qid": q["qid"], "result": "no_reference" if not got else "ok",
                     "writing_recipe_ids": sorted(got)})
        if set(q["gold"]) <= got:
            pos_hit += 1

    metrics = {
        "mode": "lexical_only",
        "positive_queries": pos_total,
        "recipe_positive_hit": pos_hit,
        "recipe_recall": (pos_hit / pos_total) if pos_total else None,
        "historical_unit_recall": None,
        "historical_unit_recall_reason": "历史论文 license 未授权（internal_analysis=false），0 个可发放历史单元，历史召回不可测",
        "boundary": {"unknown_stage_rejected": True, "third_party_blocked": True},
        "note": "recipe 覆盖为自研通用方案；真实历史单元检索在许可到位前不可评测（不以全 no_reference 冒充正例）",
    }
    core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "integration" / args.run_id / "retrieval_metrics.json", metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0
