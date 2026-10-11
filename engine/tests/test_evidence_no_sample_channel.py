"""证据渲染的"零样本 ≠ 零失败"守护（G41 证据面的另一侧）。

缺陷形状：`success_rate()` 在**没有该通道样本**时返回 `0.0`（含义是"没测过"），而渲染层把它
照原样印出来 → 报告里出现「真实 Key 通道成功率：0.00%」。在 10-24/11-14 判定报告的读者
（含架构评审）眼里，这就是"真实通道全挂"。§3 更糟：FakeLLM 零样本时它写「无失败样本。」——
一条**看起来是好消息**的空断言。

这与本项目反复登记的"没样本被读成零结果"同源，只是方向相反：那次是比率缺分母，这次是
**空分母被印成了一个数**。判据统一成两条：
① 比率一律带分母（`N/M`）；② 零样本一律写"无样本/未测量"，不写 0.00%、也不写"无失败"。
"""

import json
from pathlib import Path

from engine.regression.evidence import EvidenceWriter, render_markdown
from engine.regression.runner import (
    CHANNEL_BASELINE,
    CHANNEL_REAL,
    AcceptanceReport,
    ProblemResult,
)


def _result(
    business_id: str,
    *,
    channel: str = CHANNEL_BASELINE,
    passed: bool = True,
    failure_module: str | None = None,
    category: str = "优化类",
) -> ProblemResult:
    return ProblemResult(
        business_id=business_id,
        category=category,
        channel=channel,
        passed=passed,
        failure_module=failure_module,
        failure_detail="" if passed else "沙箱执行失败",
        stages_passed=4 if passed else 2,
        stage_records=(),
        prompt_tokens=10,
        completion_tokens=20,
        paper_sha256=None,
        resumed=False,
        kill_after_stage=None,
        executed_stages=(),
        solve_path="stage_level" if passed else None,
        isolation_mode="subprocess" if passed else None,
        dispatch_count=None,
    )


def _rate_values(markdown: str) -> list[str]:
    """§1 里三条"成功率："行的值（判据盯值本身，不被正文其它百分数干扰）。"""
    return [
        line.split("成功率：", 1)[1]
        for line in markdown.splitlines()
        if line.startswith("- ") and "成功率：" in line
    ]


# ----------------------------------------------------------------------
# §1 比率带分母 / 零样本不印成数字
# ----------------------------------------------------------------------
def test_baseline_only_run_does_not_print_real_key_zero_percent() -> None:
    """只有 FakeLLM 样本时，真实 Key 那一行必须写"无样本"，绝不能是 0.00%。"""
    report = AcceptanceReport(results=[_result("p1"), _result("p2")])

    markdown = render_markdown(report)

    assert "真实 Key 通道成功率：—（无样本，未测量）" in markdown
    values = _rate_values(markdown)
    assert not any(v.startswith("0.00%") for v in values), f"零样本被印成了一个数：{values}"
    assert any(v.startswith("100.00%（2/2）") for v in values), "比率必须带分母"


def test_rate_carries_its_denominator_when_samples_exist() -> None:
    """部分通过也要能读出分母：只有比率没有 N/M 的报告无法复核。"""
    report = AcceptanceReport(
        results=[
            _result("p1"),
            _result("p2"),
            _result("p3", passed=False, failure_module="sandbox"),
        ]
    )

    markdown = render_markdown(report)

    assert "FakeLLM 通道成功率：66.67%（2/3）" in markdown
    assert "| sandbox | 1 |" in markdown  # 有样本有失败 → 正常渲染归因表


def test_empty_report_prints_no_percentages_at_all() -> None:
    """空报告（单测/骨架通道常见）：三行比率全部写成无样本。"""
    markdown = render_markdown(AcceptanceReport())

    values = _rate_values(markdown)
    assert len(values) == 3, values
    assert all(v.startswith("—（无样本，未测量）") for v in values), values
    assert "无失败样本" not in markdown


# ----------------------------------------------------------------------
# §2/§3 空通道的措辞（"无失败"是最坏的一种空断言）
# ----------------------------------------------------------------------
def test_real_key_only_run_does_not_claim_no_failures_for_baseline() -> None:
    """真实 Key 单跑（判定通道的实际形态）时，§2/§3 不得写出"无失败样本。"。"""
    report = AcceptanceReport(
        results=[_result("r1", channel=CHANNEL_REAL, passed=False, failure_module="adapter")]
    )

    markdown = render_markdown(report)

    assert "无失败样本" not in markdown, "零样本被写成好消息=假绿灯"
    assert "这里不是「无失败」，而是没测过" in markdown
    assert "本节不适用，不是零失败" in markdown
    assert "| adapter | 1 |" not in markdown  # 归因表按通道走，不混真实通道样本
    assert "r1" in markdown  # 逐题明细仍然要看得见（不藏失败样本）


def test_failure_section_states_its_sample_size_when_clean() -> None:
    """有样本且全通过时，"无失败"必须带样本数，才是可核对的陈述。"""
    report = AcceptanceReport(results=[_result("p1"), _result("p2"), _result("p3")])

    markdown = render_markdown(report)

    assert "无失败样本（FakeLLM 通道 3 题全部通过）" in markdown


def test_channel_split_is_visible_in_summary() -> None:
    """总体那行要能一眼看出两个通道各自的通过/样本数。"""
    report = AcceptanceReport(
        results=[_result("p1"), _result("r1", channel=CHANNEL_REAL)]
    )

    markdown = render_markdown(report)

    assert "fakellm 1/1" in markdown
    assert "real_key 1/1" in markdown


# ----------------------------------------------------------------------
# summary.json：比率字段旁必须有分母字段（消费方读的是 JSON，不只是 md）
# ----------------------------------------------------------------------
def test_summary_json_carries_sample_counts(tmp_path: Path) -> None:
    report = AcceptanceReport(
        results=[_result("p1"), _result("r1", channel=CHANNEL_REAL, passed=False,
                                      failure_module="adapter")]
    )
    evidence = EvidenceWriter(tmp_path / "ev")

    summary_path, _ = evidence.write_summary(report)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert summary["samples"] == {"all": 2, "fakellm": 1, "real_key": 1}
    assert summary["success_rate_real_key"] == 0.0  # 数值本身不动（消费方兼容），分母在旁边补上
    assert summary["success_rate_baseline"] == 1.0

    empty = json.loads(EvidenceWriter(tmp_path / "ev2").write_summary(AcceptanceReport())[0]
                       .read_text(encoding="utf-8"))
    assert empty["samples"] == {"all": 0, "fakellm": 0, "real_key": 0}
