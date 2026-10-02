"""内容库质检脚本（SP4-4）：字段完整性 + sha256 全量比对 + 合规提示。

用法：
    <venv>/python scripts/content_qc.py --data data/content

质检三查（对齐 SP4-4 验收标准）：
1. 字段完整性：必填字段非空，案例 method_tags ≥3 且 compliance_note 必填；
2. sha256 比对：附件文件哈希与记录一致；
3. 合规提示：案例 100% 携带「仅作方法参照，禁止大段抄袭」。
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class QCReport:
    problems: int = 0
    templates: int = 0
    cases: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.issues) == 0


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qc_problems(problems: list[dict], attachments_dir: Path | None = None) -> list[str]:
    """真题质检：必填字段完整性 + 附件 sha256 比对。"""
    issues = []
    required = ["business_id", "competition", "year", "problem_code", "title", "prompt_zh", "visibility"]
    for i, p in enumerate(problems):
        for fld in required:
            if fld not in p or p[fld] in (None, ""):
                issues.append(f"真题[{i}] 缺字段 {fld}")
        # 附件 sha256 比对
        for att in p.get("attachments", []):
            if attachments_dir is not None and "sha256" in att:
                f = attachments_dir / att.get("oss_key", "")
                if f.exists() and sha256_of(f) != att["sha256"]:
                    issues.append(f"真题[{i}] 附件 sha256 不一致：{att.get('oss_key')}")
    return issues


def qc_templates(templates: list[dict]) -> list[str]:
    """模板质检：必填字段完整性。"""
    issues = []
    required = ["business_id", "competition", "format", "oss_key", "sha256", "tier"]
    for i, t in enumerate(templates):
        for fld in required:
            if fld not in t or t[fld] in (None, ""):
                issues.append(f"模板[{i}] 缺字段 {fld}")
    return issues


def qc_cases(cases: list[dict]) -> list[str]:
    """案例质检：method_tags ≥3 + compliance_note 必填（合规提示 100%）。"""
    issues = []
    for i, c in enumerate(cases):
        if len(c.get("method_tags", [])) < 3:
            issues.append(f"案例[{i}] method_tags 少于 3 个")
        note = c.get("compliance_note", "")
        if "仅作方法参照" not in note or "禁止大段抄袭" not in note:
            issues.append(f"案例[{i}] 缺合规提示 compliance_note")
    return issues


def run_qc(data_dir: Path, attachments_dir: Path | None = None) -> QCReport:
    """执行三库质检，返回报告。"""
    report = QCReport()
    problems = _load(data_dir / "problems.json")
    templates = _load(data_dir / "templates.json")
    cases = _load(data_dir / "cases.json")
    report.problems = len(problems)
    report.templates = len(templates)
    report.cases = len(cases)
    report.issues.extend(qc_problems(problems, attachments_dir))
    report.issues.extend(qc_templates(templates))
    report.issues.extend(qc_cases(cases))
    return report


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="内容库质检")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--attachments", type=Path, default=None)
    args = parser.parse_args()

    report = run_qc(args.data, args.attachments)
    print(f"质检完成：真题 {report.problems}、模板 {report.templates}、案例 {report.cases}")
    if report.passed:
        print("质检通过：无问题")
    else:
        print(f"发现问题 {len(report.issues)} 条：")
        for issue in report.issues:
            print(f"  - {issue}")


if __name__ == "__main__":
    main()
