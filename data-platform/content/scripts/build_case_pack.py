"""案例种子数据包构建（工作包3）：标注报告 → 灌库数据包 + 溯源清单。

输入：out/cases_report.json（annotate_cases.py 产出）+ 数据集源 PDF
输出（out/pack/）：
- cases.json                   灌库数据包（字段严格对齐 server ports.CaseRecord，供 content_seed.py 直读）
- cases_seed_v0_sources.json   溯源清单（源文件 sha256/标注证据/合规与授权提示）

用法：python scripts/build_case_pack.py [--report out/cases_report.json]
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CASES_CONFIG = CONTENT_DIR / "config" / "cases_v0.yaml"
DEFAULT_REPORT = CONTENT_DIR / "out" / "cases_report.json"
PACK_DIR = CONTENT_DIR / "out" / "pack"

# 与 server/app/domain/entitlement/ports.py::CaseRecord 字段严格一致（多/少一个键灌库即报错）
CASE_FIELDS = (
    "business_id", "problem_id", "title", "award", "method_tags",
    "oss_key", "sha256", "compliance_note",
)
# content_qc.py 校验：必须同时含以下两短语；迁移 0009 上限 256
COMPLIANCE_NOTE = "本案例仅作方法参照与学习交流使用，禁止大段抄袭或直接复制；引用时请注明原作者与出处。"
COMPLIANCE_REQUIRED = ("仅作方法参照", "禁止大段抄袭")
# 源目录 papers_pending_auth 表明授权核验未完成，仅限内部使用
LICENSE_NOTE = "来源目录 papers_pending_auth：授权/署名核验完成前仅限内部评测使用，不得对外分发"

FIELD_MAX = {"business_id": 64, "problem_id": 64, "title": 128, "award": 32, "oss_key": 256, "sha256": 64}

EXTRACT_TOOL = "pdftotext -enc UTF-8 -layout -"


def reconfigure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(report_path, cfg_path):
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    root = Path(os.environ.get("ERDOS_DATA_ROOT", cfg.get("dataset_root", "D:/Erdos_data")))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    review_set = set(report.get("review", []))

    errors = []
    warnings = list(report.get("id_conflicts", []))
    cases = []
    sources = []
    seen = set()

    for it in report["items"]:
        # 与 annotate_cases.py 的合格口径一致：business_id + title + tags ≥3
        if not (it.get("business_id") and it.get("title") and len(it.get("tags", [])) >= 3):
            continue
        business_id = it["business_id"]
        if business_id in seen:
            errors.append("business_id 重复：{}（{}）".format(business_id, it["rel_path"]))
            continue
        seen.add(business_id)

        rel_posix = it["rel_path"]
        source_path = root / rel_posix
        if not source_path.exists():
            errors.append("{}: 源文件不存在 {}".format(business_id, rel_posix))
            continue

        record = {
            "business_id": business_id,
            "problem_id": it["problem_id"],
            "title": it["title"],
            "award": it["award"],
            "method_tags": [t["tag"] for t in it["tags"]],
            "oss_key": rel_posix,  # 预上传占位：数据集相对路径；上 OSS 后改写为 object key
            "sha256": sha256_of(source_path),
            "compliance_note": COMPLIANCE_NOTE,
        }
        if set(record.keys()) != set(CASE_FIELDS):
            errors.append("{}: 字段与 CaseRecord 不一致".format(business_id))
        for fld in ("business_id", "problem_id", "title", "award", "oss_key", "sha256", "compliance_note"):
            if not record[fld]:
                errors.append("{}: {} 为空".format(business_id, fld))
        for fld, limit in FIELD_MAX.items():
            if len(str(record[fld])) > limit:
                errors.append("{}: {} 超长（{} > {}）".format(business_id, fld, len(str(record[fld])), limit))
        if len(record["method_tags"]) < 3:
            errors.append("{}: method_tags 少于 3".format(business_id))
        for sub in COMPLIANCE_REQUIRED:
            if sub not in record["compliance_note"]:
                errors.append("{}: compliance_note 缺 {}".format(business_id, sub))

        cases.append(record)
        sources.append({
            "business_id": business_id,
            "problem_id": it["problem_id"],
            "source": {
                "rel": rel_posix,
                "sha256": record["sha256"],
                "bytes": source_path.stat().st_size,
                "extract_tool": EXTRACT_TOOL,
            },
            "annotation": {
                "title_from": it.get("title_from"),
                "title_candidates": it.get("title_candidates", []),
                "keywords": it.get("keywords", ""),
                "tags": it.get("tags", []),
                "award": it.get("award"),
                "award_evidence": it.get("award_evidence", []),
                "text_chars": it.get("text_chars", 0),
                "flags": it.get("flags", []),
            },
            "review_priority": rel_posix in review_set,
        })

    cases.sort(key=lambda c: c["business_id"])
    sources.sort(key=lambda s: s["business_id"])
    return cfg, root, report, cases, sources, errors, warnings


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="案例种子数据包构建")
    parser.add_argument("--config", type=Path, default=DEFAULT_CASES_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    cfg, root, report, cases, sources, errors, warnings = build(args.report, args.config)

    if warnings:
        print("警告 {} 条：".format(len(warnings)))
        for w in warnings:
            print("  - {}".format(w))
    if errors:
        print("构建失败，{} 条错误：".format(len(errors)))
        for err in errors:
            print("  - " + err)
        sys.exit(1)

    generated_at = utc_now()
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    (PACK_DIR / "cases.json").write_bytes(
        (json.dumps(cases, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (PACK_DIR / "cases_seed_v0_sources.json").write_bytes(
        (json.dumps({
            "pack": "cases_seed_v0",
            "generated_at": generated_at,
            "dataset_root": str(root),
            "annotation_report": str(args.report.resolve().relative_to(CONTENT_DIR.resolve()).as_posix()),
            "license_note": LICENSE_NOTE,
            "review_note": "review_priority=true 条目为低边际置信（标签数少/标题多候选/人工修正），抽检优先",
            "items": sources,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    review_n = sum(1 for s in sources if s["review_priority"])
    by_comp = {}
    for c in cases:
        by_comp.setdefault(c["problem_id"].split("-")[0], 0)
        by_comp[c["problem_id"].split("-")[0]] += 1
    print("构建完成：")
    print("  cases.json                  {} 篇（复核优先级 {} 篇）".format(len(cases), review_n))
    print("  cases_seed_v0_sources.json  溯源 {} 条".format(len(sources)))
    print("  按赛事分布：{}".format(", ".join("{}={}".format(k, v) for k, v in sorted(by_comp.items()))))
    print("输出目录：{}".format(PACK_DIR))


if __name__ == "__main__":
    main()
