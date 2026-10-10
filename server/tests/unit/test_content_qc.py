"""内容库质检测试（SP4-4）：字段完整性 + sha256 比对 + 合规提示。"""

import hashlib

from scripts.content_qc import qc_cases, qc_problems, qc_templates, run_qc


def _problem(**overrides) -> dict:
    base = {
        "business_id": "cumcm-2024-A", "competition": "cumcm", "year": 2024,
        "problem_code": "A", "title": "题", "prompt_zh": "题干", "visibility": "public",
        "attachments": [],
    }
    base.update(overrides)
    return base


def _template(**overrides) -> dict:
    base = {
        "business_id": "tpl-1", "competition": "cumcm", "format": "latex",
        "oss_key": "oss/t.tex", "sha256": "a" * 64, "tier": "free",
    }
    base.update(overrides)
    return base


def _case(**overrides) -> dict:
    base = {
        "business_id": "case-1", "problem_id": "p1", "title": "案例",
        "method_tags": ["方法1", "方法2", "方法3"],
        "compliance_note": "仅作方法参照，禁止大段抄袭",
    }
    base.update(overrides)
    return base


def test_qc_problems_complete() -> None:
    """字段完整：无问题。"""
    assert qc_problems([_problem()]) == []


def test_qc_problems_missing_field() -> None:
    """缺字段：检出。"""
    issues = qc_problems([_problem(title="")])
    assert any("title" in i for i in issues)


def test_qc_problems_sha256_mismatch(tmp_path) -> None:
    """附件 sha256 不一致：检出。"""
    f = tmp_path / "data.txt"
    f.write_text("hello", encoding="utf-8")
    good_sha = hashlib.sha256(b"hello").hexdigest()
    bad_sha = "0" * 64
    problems = [
        _problem(attachments=[{"name": "data.txt", "oss_key": "data.txt", "sha256": bad_sha}])
    ]
    assert len(qc_problems(problems, tmp_path)) == 1  # sha256 不一致检出
    # 正确 sha256 无问题
    problems[0]["attachments"][0]["sha256"] = good_sha
    assert qc_problems(problems, tmp_path) == []


def test_qc_templates_complete_and_missing() -> None:
    """模板：完整无问题，缺字段检出。"""
    assert qc_templates([_template()]) == []
    assert len(qc_templates([_template(sha256="")])) == 1


def test_qc_cases_compliance_note() -> None:
    """案例合规提示：缺 compliance_note 检出。"""
    assert qc_cases([_case()]) == []
    issues = qc_cases([_case(compliance_note="")])
    assert any("compliance_note" in i for i in issues)


def test_qc_cases_method_tags_min_3() -> None:
    """案例 method_tags <3 检出。"""
    issues = qc_cases([_case(method_tags=["方法1"])])
    assert any("method_tags" in i for i in issues)


def test_run_qc_full(tmp_path) -> None:
    """完整质检：示例数据包通过。"""
    import json

    data_dir = tmp_path / "content"
    data_dir.mkdir()
    (data_dir / "problems.json").write_text(
        json.dumps([_problem()], ensure_ascii=False), encoding="utf-8"
    )
    (data_dir / "templates.json").write_text(
        json.dumps([_template()], ensure_ascii=False), encoding="utf-8"
    )
    (data_dir / "cases.json").write_text(
        json.dumps([_case()], ensure_ascii=False), encoding="utf-8"
    )
    report = run_qc(data_dir)
    assert report.passed is True
    assert report.problems == 1
    assert report.templates == 1
    assert report.cases == 1
