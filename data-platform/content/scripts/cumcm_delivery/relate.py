"""阶段4：复用既有已核验结果（stage04 3题/7篇 + codex v6 题面/论文画像），重组 problems/subproblems/papers/links。

不重新编号、不重算画像；未确认父题的论文保留 unresolved，不按文件名强配。
"""
import json

from . import core


def main(args):
    run_dir = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id

    s4 = core.load_json(core.CONTENT_DIR / "normalized" / "stage04" / "cumcm" / "bundle.json")
    v6 = core.load_json(core.CONTENT_DIR / "normalized" / "competitions_codex_v6" / "bundle.json")

    # 题目（stage04 3题 + v6 cumcm 7题，按 problem_id 去重）
    problems = {}
    for p in s4["problems"] + [p for p in v6["problems"] if p.get("competition_id") == "cumcm"]:
        problems[p["problem_id"]] = p

    subproblems = []
    for p in s4["problems"]:
        for s in p.get("subproblems", []):
            subproblems.append({
                "subproblem_id": s["subproblem_id"], "problem_id": p["problem_id"],
                "problem_types": s.get("problem_types", []), "method_tags": s.get("method_tags", []),
                "data_tags": s.get("data_tags", []), "domain_tags": s.get("domain_tags", []),
                "requirement": s.get("source"), "evidence": s.get("provenance"),
            })

    # 论文（v6 cumcm 34篇 + stage04 7篇，去重）
    papers = {}
    for c in v6["cases"]:
        if c.get("competition_id") == "cumcm":
            papers[c["case_id"]] = c
    for c in s4["cases"]:
        papers[c["case_id"]] = c

    # 父题关联：确认来自 stage04 的 case.problem_id（7篇）；其余 unresolved
    confirmed = {}
    unresolved = []
    for cid, c in papers.items():
        if c.get("problem_id") and c.get("resolution_status") == "resolved":
            confirmed[cid] = {"case_id": cid, "problem_id": c["problem_id"],
                              "relation_status": "confirmed", "evidence": {"source": "stage04 resolved"}}
        else:
            unresolved.append({"case_id": cid, "problem_id": c.get("problem_id"),
                               "relation_status": "unresolved",
                               "evidence": None, "reason": "未内容复核确认父题"})

    links = list(confirmed.values()) + unresolved

    core.write_jsonl(run_dir / "problems.jsonl", list(problems.values()))
    core.write_jsonl(run_dir / "subproblems.jsonl", subproblems)
    core.write_jsonl(run_dir / "papers.jsonl", list(papers.values()))
    core.write_jsonl(run_dir / "paper_problem_links.jsonl", links)

    core.write_json(run_dir / "identity_review.json", {
        "confirmed_parent_links": len(confirmed),
        "unresolved_papers": len(unresolved),
        "problems_with_subproblems": sum(1 for p in problems.values() if p.get("subproblems")),
        "note": "已核验身份复用；其余论文父题未内容复核，保留 unresolved",
        "confirmed_examples": sorted(confirmed.keys()),
    })
    core.write_json(run_dir / "unresolved_links.json", {"count": len(unresolved), "links": unresolved})

    print("relate 完成：题={}（{} 有小问），小问={}，论文画像={}，确认父题={}，未确认={}".format(
        len(problems), sum(1 for p in problems.values() if p.get("subproblems")),
        len(subproblems), len(papers), len(confirmed), len(unresolved)))
    return 0
