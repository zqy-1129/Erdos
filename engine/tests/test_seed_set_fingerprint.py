"""整集冻结指纹接线守护（G46）。

`engine/regression/problems.py` 的 `manifest()` 在自己的 docstring 里承诺了两件事：它是 11-05
冻结清单的校验依据，且"判定报告附录引用"。承诺在 10-12 审计前一直是**空的**——生产链路零调用方，
`summary.json` 与 `report.md` 里没有任何摘要字段。审计员只能凭"看着是同一个仓库"相信样本没变。

这里盯三条接线判据：
① 指纹进 `summary.json` / `report.md`，而且是**整集**（20 题）不是本次抽样子集——冒烟跑
   （`--per-category`）默认写进同一个证据目录，覆盖 20 题基线，它同样得能证明样本没变；
② 摘要覆盖的字段集 == 入库记录 `to_seed_record()` 的字段集（新字段不许静默逃出冻结哈希，
   那种"全绿"比报错更坏：校验看起来做了，其实没看那个字段）；
③ 报告显式写「本次抽样 M 题 / 整集 N 题」，被冒烟跑覆盖后的 `report.md` 不能长得像全量跑。
"""

import hashlib
import json

from engine.regression.evidence import EvidenceWriter, render_markdown
from engine.regression.problems import (
    REGRESSION_SET,
    RegressionProblem,
    _statement_digest,
    manifest,
    stratified_sample,
)
from engine.regression.runner import CHANNEL_BASELINE, AcceptanceReport, ProblemResult


def _result(business_id: str, category: str) -> ProblemResult:
    return ProblemResult(
        business_id=business_id,
        category=category,
        channel=CHANNEL_BASELINE,
        passed=True,
        failure_module=None,
        failure_detail="",
        stages_passed=4,
        stage_records=(),
        prompt_tokens=1,
        completion_tokens=1,
        paper_sha256=None,
        resumed=False,
        kill_after_stage=None,
        executed_stages=(),
    )


def _report(problems: tuple[RegressionProblem, ...]) -> AcceptanceReport:
    return AcceptanceReport(
        results=[_result(p.business_id, p.category) for p in problems]
    )


def test_seed_set_covers_every_field_of_the_seed_record() -> None:
    """逐题哈希的输入 = 落到证据里的那条入库记录（两侧不是各写一遍才算一致）。"""
    problem = REGRESSION_SET[0]
    canonical = json.dumps(
        problem.to_seed_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    entry = next(
        e for e in manifest()["problems"] if e["business_id"] == problem.business_id
    )
    assert entry["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_new_seed_record_field_is_not_silently_out_of_the_digest(monkeypatch) -> None:
    """给入库记录加字段，冻结哈希必须跟着变（G46② 的漂移方向）。

    反证对象是一份独立维护的字段清单：加了 `to_seed_record()` 却忘了加进摘要 → 哈希不变 →
    冻结校验全绿，而新字段从没被校验过。
    """
    original = REGRESSION_SET[0]
    before = _statement_digest(original)
    real = RegressionProblem.to_seed_record
    monkeypatch.setattr(
        RegressionProblem, "to_seed_record",
        lambda self: {**real(self), "rubric_version": "r-2026-11"},
    )
    assert _statement_digest(original) != before, "新增字段逃过了冻结哈希，冻结校验会假装全绿"


def test_summary_carries_full_set_fingerprint_even_for_smoke_run(tmp_path) -> None:
    """本次只跑 4 题，`summary.json` 里的指纹仍是整集 20 题（G46①）。"""
    smoke = stratified_sample(1)
    assert len(smoke) < len(REGRESSION_SET)
    evidence = EvidenceWriter(tmp_path / "ev")
    summary_path, _ = evidence.write_summary(_report(smoke))
    seed = json.loads(summary_path.read_text(encoding="utf-8"))["seed_set"]

    assert seed["count"] == len(REGRESSION_SET) == 20
    assert len(seed["set_sha256"]) == 64
    assert sum(seed["categories"].values()) == 20
    assert {e["business_id"] for e in seed["problems"]} == {
        p.business_id for p in REGRESSION_SET
    }
    assert all(len(e["sha256"]) == 64 for e in seed["problems"])


def test_report_labels_sampled_against_full_set() -> None:
    """冒烟跑的报告要写出「本次抽样 4 题 / 整集 20 题」＋整集题型分桶（G46③）。"""
    markdown = render_markdown(_report(stratified_sample(1)))

    assert "**本次抽样 4 题 / 整集 20 题**" in markdown
    assert manifest()["set_sha256"] in markdown
    for category, count in manifest()["categories"].items():
        assert f"{category} {count}" in markdown


def test_report_says_full_set_when_nothing_was_sampled() -> None:
    """正向对照：全量跑不能也写成"抽样"，否则这句标注在两处都不可信。"""
    markdown = render_markdown(_report(REGRESSION_SET))

    assert "本次 20 题 = 整集 20 题（全量）" in markdown
    assert "本次抽样" not in markdown


def test_empty_report_says_no_sample_not_a_zero_problem_sample() -> None:
    """零样本沿用 §1 的口径：写"无样本"，不写"抽样 0 题"（后者会被读成抽了却全空）。"""
    markdown = render_markdown(AcceptanceReport())

    assert "本次**无样本**（未跑任何题）/ 整集 20 题" in markdown
    assert "抽样 0 题" not in markdown
