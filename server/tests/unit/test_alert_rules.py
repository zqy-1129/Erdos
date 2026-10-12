"""值班告警规则与服务端代码的漂移守护：`deploy/prometheus-alert-rules.yml`。

为什么要有这条：《服务端架构》§10 把可用性 99.5% / 错误率 >1% / P95 >500ms 当成产品承诺，
服务端代码里有同名配置（`SLO_AVAILABILITY`、`alert_error_rate`、`alert_p95_ms`）。规则文件
如果只留在某个环境的部署目录里，就会出现"代码改了阈值、值班还在按旧阈值呼叫"这类事故——
而这正是本项目反复栽过的"两处各写一遍，改一处漏一处"。

守护分三层：指标名必须真实导出（改名/删指标立刻红）、阈值字面量必须等于代码默认值、
每条规则必须带档位与 runbook（值班点开就能查）。
"""

import re
from pathlib import Path
from typing import Any

import yaml
from prometheus_client import generate_latest

from app.core.config import Settings
from app.domain.alerts.severity import SLO_AVAILABILITY, Severity

RULES_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prometheus-alert-rules.yml"


def _document() -> dict[str, Any]:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _rules() -> list[dict[str, Any]]:
    return [rule for group in _document()["groups"] for rule in group["rules"]]


def _exported_names() -> set[str]:
    """当前进程真实导出的指标族名（`# TYPE` 行即注册表内容，无需私有 API）。"""
    return {
        line.split()[2]
        for line in generate_latest().decode("utf-8").splitlines()
        if line.startswith("# TYPE ")
    }


def _unknown_metrics(expr: str) -> set[str]:
    """表达式里引用了但从未导出的 erdos_* 指标。

    直方图/摘要会派生 `_bucket`/`_sum`/`_count` 等序列，所以"以某个族名开头 + `_`"也算已导出。
    """
    exported = _exported_names()
    referenced = set(re.findall(r"\berdos_[a-z0-9_]+", expr))
    return {
        name
        for name in referenced
        if not any(name == candidate or name.startswith(f"{candidate}_") for candidate in exported)
    }


def test_rules_file_parses_with_expected_shape() -> None:
    rules = _rules()
    assert len(rules) >= 5, "规则数量骤减通常意味着整组被误删"
    for rule in rules:
        assert rule.get("alert"), rule
        assert rule.get("expr"), rule["alert"]
        assert rule["labels"].get("severity") in {s.value for s in Severity}, rule["alert"]
        assert rule["annotations"].get("summary"), rule["alert"]
        assert rule["annotations"].get("runbook"), f"{rule['alert']} 缺 runbook，值班无从下手"


def test_every_referenced_metric_is_exported() -> None:
    """规则引用了不存在的指标 = 这条告警永远不会响，比不写更坏。"""
    offenders = {rule["alert"]: _unknown_metrics(rule["expr"]) for rule in _rules()}
    bad = {name: metrics for name, metrics in offenders.items() if metrics}
    assert not bad, f"以下规则引用了未导出的指标：{bad}"


def test_unknown_metric_helper_actually_detects_drift() -> None:
    """守护本身要能被测坏：改名后的旧指标名必须被判为未知。"""
    assert _unknown_metrics("sum(rate(erdos_alert_routed_total[5m]))") == set()
    assert _unknown_metrics("sum(rate(erdos_alert_routed_totoal[5m]))") == {
        "erdos_alert_routed_totoal"
    }


def test_thresholds_match_code_defaults() -> None:
    """阈值字面量必须与服务端配置同源，否则改了代码而值班仍按旧阈值呼叫。"""
    settings = Settings(env="test")
    by_name = {rule["alert"]: rule["expr"] for rule in _rules()}
    budget = f"{1.0 - SLO_AVAILABILITY:.3f}"  # 0.995 → 0.005
    assert f"14.4 * {budget}" in by_name["ErdosAvailabilityBurnFast"]
    assert f"6 * {budget}" in by_name["ErdosAvailabilityBurnSlow"]
    assert f"> {settings.alert_error_rate:g}" in by_name["ErdosHttpErrorRateHigh"]
    assert f"> {settings.alert_p95_ms / 1000:g}" in by_name["ErdosLatencyP95High"]


def test_latency_rule_states_its_p95_substitution() -> None:
    """PRD 文字是 P99，实现与规则都是 P95（口径待裁决）——规则文件里必须写明，不假装。"""
    text = RULES_PATH.read_text(encoding="utf-8")
    latency_block = text[text.index("ErdosLatencyP95High") :]
    assert "P95" in latency_block and "P99" in latency_block
