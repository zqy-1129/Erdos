"""内容库质检（工作包4）：字段 / 下载（可定位） / 哈希 三查，DE 自检 + QA 抽检共用。

用法：
    <venv>/python scripts/content_qc.py --data out/pack --attachments D:/Erdos_data            # DE 自检（全量）
    <venv>/python scripts/content_qc.py --data out/pack --attachments D:/Erdos_data --sample 10 --seed 7
                                                                                              # QA 抽检（每库抽 10 条验哈希）
    <venv>/python scripts/content_qc.py --data out/pack --attachments D:/Erdos_data --json-report out/qc_report.json

三查口径（对齐 SP4-4 验收标准）：
1. 字段：三库必填字段非空 + 键集与 server ports.py 记录一一对应 + 长度上限（迁移 0009）；
   案例 method_tags ≥3 且 compliance_note 含合规短语（100% 携带）。
2. 下载：oss_key 可定位到本地产物（预上传占位语义——真题/案例/附件相对数据集根，模板相对 out/）；
   上 OSS 后此处改为对象可达性核验。
3. 哈希：sha256 重算比对（--sample 控制抽检规模，默认全量；字段查不受抽样影响）。

退出码：0 通过 / 1 有问题（供 CI 与人工抽检复用）。脚本兼容 Python 3.8（DE 本机 venv）。
"""

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与 server/app/domain/entitlement/ports.py 的记录字段严格一致（多/少键即报错）
PROBLEM_FIELDS = {
    "business_id", "competition", "year", "problem_code", "title", "tags",
    "prompt_zh", "prompt_en", "attachments", "scoring", "dataset_hint", "visibility",
}
TEMPLATE_FIELDS = {
    "business_id", "competition", "format", "oss_key", "sha256", "version", "changelog", "tier",
}
CASE_FIELDS = {
    "business_id", "problem_id", "title", "award", "method_tags",
    "oss_key", "sha256", "compliance_note",
}

PROBLEM_REQUIRED = ("business_id", "competition", "year", "problem_code", "title", "prompt_zh", "visibility")
TEMPLATE_REQUIRED = ("business_id", "competition", "format", "oss_key", "sha256", "version", "tier")
CASE_REQUIRED = ("business_id", "problem_id", "title", "award", "method_tags", "oss_key", "sha256", "compliance_note")

# 迁移 0009 / 数据模型 8.1 上限
FIELD_MAX = {
    "problems": {"business_id": 64, "title": 128, "prompt_zh": 8192},
    "templates": {"business_id": 64, "oss_key": 256, "sha256": 64},
    "cases": {"business_id": 64, "problem_id": 64, "title": 128, "award": 32, "oss_key": 256, "sha256": 64},
}
COMPLIANCE_REQUIRED = ("仅作方法参照", "禁止大段抄袭")


def reconfigure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def check_fields(name, records, field_set, required, limits, issues):
    """字段查：键集 + 必填 + 长度上限。返回通过数。"""
    ok = 0
    for i, rec in enumerate(records):
        rec_id = rec.get("business_id") or "#{}".format(i)
        bad = False
        if set(rec.keys()) != field_set:
            issues.append("{}[{}] 键集与契约不一致（多 {} / 少 {}）".format(
                name, rec_id, sorted(set(rec) - field_set), sorted(field_set - set(rec))))
            bad = True
        for fld in required:
            if rec.get(fld) in (None, ""):
                issues.append("{}[{}] 缺字段 {}".format(name, rec_id, fld))
                bad = True
        for fld, limit in limits.items():
            if fld in rec and len(str(rec[fld])) > limit:
                issues.append("{}[{}] {} 超长（{} > {}）".format(name, rec_id, fld, len(str(rec[fld])), limit))
                bad = True
        if not bad:
            ok += 1
    return ok


def check_case_compliance(cases, issues):
    for i, c in enumerate(cases):
        rec_id = c.get("business_id") or "#{}".format(i)
        if len(c.get("method_tags", [])) < 3:
            issues.append("cases[{}] method_tags 少于 3 个".format(rec_id))
        note = c.get("compliance_note", "")
        if any(sub not in note for sub in COMPLIANCE_REQUIRED):
            issues.append("cases[{}] compliance_note 缺合规短语（需含 {}）".format(rec_id, "、".join(COMPLIANCE_REQUIRED)))


def check_hash(name, records, root, issues, checked):
    """下载+哈希查：oss_key 可定位且 sha256 一致。checked 计数写入。"""
    for i, rec in enumerate(records):
        rec_id = rec.get("business_id") or "#{}".format(i)
        oss_key = rec.get("oss_key") or ""
        path = root / oss_key
        if not oss_key or not path.exists():
            issues.append("{}[{}] oss_key 不可定位：{}".format(name, rec_id, oss_key or "<空>"))
            continue
        if rec.get("sha256") and sha256_of(path) != rec["sha256"]:
            issues.append("{}[{}] sha256 不一致：{}".format(name, rec_id, oss_key))
            continue
        checked[0] += 1


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="内容库质检（字段/下载/哈希三查）")
    parser.add_argument("--data", type=Path, required=True, help="数据包目录（含 problems/templates/cases.json）")
    parser.add_argument("--attachments", type=Path, default=None, help="数据集根目录（真题附件/案例 PDF 相对此路径）")
    parser.add_argument("--templates-root", type=Path, default=None,
                        help="模板产物根目录（oss_key 形如 templates/xxx.zip）；默认取 data 同级 templates/")
    parser.add_argument("--sample", type=int, default=0, help="每库哈希抽检条数；0=全量（默认）")
    parser.add_argument("--seed", type=int, default=0, help="抽检随机种子（复现用）")
    parser.add_argument("--json-report", type=Path, default=None, help="输出机器可读报告")
    args = parser.parse_args()

    data_dir = args.data
    attachments = args.attachments
    templates_root = args.templates_root or (data_dir.resolve().parent)
    problems = load_json(data_dir / "problems.json")
    templates = load_json(data_dir / "templates.json")
    cases = load_json(data_dir / "cases.json")

    issues = []
    rng = random.Random(args.seed)

    def sample(records):
        if args.sample and len(records) > args.sample:
            return rng.sample(records, args.sample)
        return records

    # ---- 1. 字段查（全量，不受抽样影响）----
    ok_problems = check_fields("problems", problems, PROBLEM_FIELDS, PROBLEM_REQUIRED,
                               FIELD_MAX["problems"], issues)
    ok_templates = check_fields("templates", templates, TEMPLATE_FIELDS, TEMPLATE_REQUIRED,
                                FIELD_MAX["templates"], issues)
    ok_cases = check_fields("cases", cases, CASE_FIELDS, CASE_REQUIRED, FIELD_MAX["cases"], issues)
    check_case_compliance(cases, issues)

    # ---- 2/3. 下载 + 哈希查（按抽检规模）----
    checked = [0]
    if attachments is not None:
        for p in sample(problems):
            pid = p.get("business_id")
            for att in p.get("attachments", []):
                oss_key = att.get("oss_key") or ""
                path = attachments / oss_key
                tag = "problems[{}|{}]".format(pid, att.get("name", ""))
                if not oss_key or not path.exists():
                    issues.append("{} oss_key 不可定位：{}".format(tag, oss_key or "<空>"))
                    continue
                if att.get("sha256") and sha256_of(path) != att["sha256"]:
                    issues.append("{} sha256 不一致：{}".format(tag, oss_key))
                    continue
                checked[0] += 1
        check_hash("cases", sample(cases), attachments, issues, checked)
    else:
        issues.append("未提供 --attachments：跳过真题附件/案例的下载与哈希查（字段查已完成）")

    # 模板 oss_key 相对 out/ 根（templates/xxx.zip）
    check_hash("templates", sample(templates), templates_root, issues, checked)

    passed = not issues
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_dir": str(data_dir),
        "attachments_root": str(attachments) if attachments else None,
        "templates_root": str(templates_root),
        "sample": args.sample,
        "seed": args.seed,
        "counts": {"problems": len(problems), "templates": len(templates), "cases": len(cases)},
        "fields_ok": {"problems": ok_problems, "templates": ok_templates, "cases": ok_cases},
        "hashes_checked": checked[0],
        "issues": issues,
        "passed": passed,
    }
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    scope = "全量" if not args.sample else "抽检 {} 条/库（seed={}）".format(args.sample, args.seed)
    print("质检完成（{}）：真题 {}、模板 {}、案例 {}；哈希核验 {} 条".format(
        scope, len(problems), len(templates), len(cases), checked[0]))
    if passed:
        print("质检通过：字段 / 下载 / 哈希三查无问题")
    else:
        print("发现问题 {} 条：".format(len(issues)))
        for issue in issues:
            print("  - " + issue)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
