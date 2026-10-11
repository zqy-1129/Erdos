"""阈值漂移守护（W37 · EN-THRESH-SSOT 第二批：G32 里不需要新裁决的那半）。

G32 的原话是"阈值多处重复且互相矛盾"。拆开看其实是三种病，本文件只治前两种：

① **同一承诺写两遍**（沙箱 120s / 2GB、覆盖率 85、rubric 0.70 四份）——可以立刻机器互证；
② **判据分母被悄悄改动**（回归集题量与题型分桶、留痕词表条数）——加下限断言，缩了当场红；
③ **四个"阶段超时"数字互不相等**（900 回归驱动等待 / 600 e2e 脚本 / 180s 客户端看门狗 /
   15s 基准目标）——**这不是同一概念的重复**，统一它等于顺手决定"到点算失败还是保留部分产物"，
   与 W26 是同一个 DEC，故本文件刻意不去"顺手收口"，只把已确认的 ①② 钉住。

另外两条纪律也在这里被执行：`quality_gate.py` 不得重复硬编码门禁值（单源在 pyproject）、
文档与报告引用的数值必须能从代码侧机器取出（`scripts/engine_fact_sheet.py` 的分区）。
"""

import re
from pathlib import Path

import pytest

from engine.gates.schema import load_rubric_for_stage
from engine.orchestrator.graph import STAGES
from engine.regression.problems import REGRESSION_SET
from engine.sandbox.constants import (
    DOCKER_MEMORY_FLAG,
    SANDBOX_MEMORY_LIMIT_MB,
    SANDBOX_TIMEOUT_SECONDS,
)
from engine.sandbox.subprocess_sandbox import SubprocessSandbox
from engine.trail.redact import TRAIL_SENSITIVE_FRAGMENTS

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "engine" / "pyproject.toml"
SERVER_PYPROJECT = REPO / "server" / "pyproject.toml"
QUALITY_GATE = REPO / "scripts" / "quality_gate.py"
RUBRIC_DIR = REPO / "engine" / "gates" / "rubrics"

# SP1-7 判定分母（QA 手册 v1.0 写的 10 题是旧口径，10-11 已登记为必改项）
MIN_PROBLEMS = 20
COVERAGE_GATE = 85
RUBRIC_THRESHOLD = 0.70


# ----------------------------------------------------------------------
# ② 判据分母不许悄悄缩
# ----------------------------------------------------------------------
def test_regression_set_meets_the_decision_denominator() -> None:
    assert len(REGRESSION_SET) >= MIN_PROBLEMS, (
        f"回归集只剩 {len(REGRESSION_SET)} 题：SP1-7 的 ≥85% 成功率分母会失真"
    )
    ids = [p.business_id for p in REGRESSION_SET]
    assert len(set(ids)) == len(ids), "business_id 重复会让同一题被算两次"


def test_every_category_can_reach_the_gate() -> None:
    """验收硬条件"每类题型至少 1 题成功"（regression/runner.py:6）要求分母四类都在。

    某类被清零＝该维度的判据被静默关掉：runner 会一直报"题型 X 无成功样本"，或更糟——
    连这一类都不再出现在统计里，谁也不会注意到少了一个维度。
    """
    from engine.regression.problems import category_distribution

    dist = category_distribution(REGRESSION_SET)
    assert set(dist) == {
        "optimization",
        "evaluation",
        "prediction",
        "mechanism",
    }, f"题型分桶与 problems.py 登记的 4 类不符：{dist}"
    assert all(v >= 1 for v in dist.values())


# ----------------------------------------------------------------------
# ① rubric 阈值四份必须一致（阈值本身未经真题校准是另一件事，记在报告里）
# ----------------------------------------------------------------------
def test_rubric_thresholds_are_identical_across_stages() -> None:
    thresholds = {}
    for stage in STAGES:
        rubric = load_rubric_for_stage(stage, RUBRIC_DIR)
        thresholds[stage] = rubric.threshold
    assert set(thresholds.values()) == {RUBRIC_THRESHOLD}, (
        f"四份 rubric 的通过阈值不再一致（{thresholds}）：0.70 是 §3.2 的登记值，"
        "改一处必须四份同改并重新校准"
    )


def test_every_rubric_weight_sum_is_one() -> None:
    """权重和必须为 1：加权总分 ≥0.70 在权重和≠1 的 rubric 上没有可比性。

    `load_rubric()`（engine/gates/schema.py:54）已经调用 `validate()`，所以"文件写坏了"
    在加载时就会抛；本用例提供**独立**判据——万一 validate() 的实现被放宽或删掉，权重和
    漂移仍会在这里变红，而不是让门禁给出一个像模像样却不可比的分数。
    """
    loader_source = (REPO / "engine" / "gates" / "schema.py").read_text(encoding="utf-8")
    assert "rubric.validate()" in loader_source, "加载器不再校验 rubric，守护只剩这一条"
    for stage in STAGES:
        rubric = load_rubric_for_stage(stage, RUBRIC_DIR)
        total = sum(d.weight for d in rubric.dimensions)
        assert abs(total - 1.0) < 1e-6, f"{stage} 权重和为 {total}"
        assert rubric.dimensions, f"{stage} 评分维度为空"
        assert 0.0 < rubric.threshold <= 1.0


# ----------------------------------------------------------------------
# ① 覆盖率门禁单源：pyproject 是权威，quality_gate 不得再抄一遍
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", [PYPROJECT, SERVER_PYPROJECT], ids=["engine", "server"])
def test_coverage_gate_is_85_in_both_pyprojects(path: Path) -> None:
    found = re.findall(r"--cov-fail-under=(\d+)", path.read_text(encoding="utf-8"))
    assert found == [str(COVERAGE_GATE)], f"{path.name} 的覆盖率门禁是 {found}，登记值 {COVERAGE_GATE}"


def test_quality_gate_does_not_restate_the_coverage_threshold() -> None:
    """一键门只跑 pytest，阈值留在 pyproject——两处各写一遍必漂移（G32 原案）。"""
    source = QUALITY_GATE.read_text(encoding="utf-8")
    assert "cov-fail-under" not in source, "quality_gate 里又抄了一遍阈值"
    assert not re.search(r"\b85\b", source), "quality_gate 里出现了硬编码 85"


# ----------------------------------------------------------------------
# ① 沙箱阈值：常量 ⇔ 实例生效值 ⇔ 事实源，三方互证（含两种内存表示）
# ----------------------------------------------------------------------
def test_sandbox_thresholds_reach_the_instance(tmp_path: Path) -> None:
    sandbox = SubprocessSandbox()
    assert sandbox._timeout == SANDBOX_TIMEOUT_SECONDS  # noqa: SLF001
    assert sandbox._memory_limit_mb == SANDBOX_MEMORY_LIMIT_MB  # noqa: SLF001
    assert SANDBOX_TIMEOUT_SECONDS == 120.0, "PRD §6.1 承诺单次执行 ≤120s"
    assert SANDBOX_MEMORY_LIMIT_MB == 2048, "PRD §6.1 承诺 ≤2GB"


def test_docker_memory_notation_matches_megabyte_constant() -> None:
    match = re.fullmatch(r"(\d+)([gm])", DOCKER_MEMORY_FLAG.strip().lower())
    assert match, f"Docker 内存规格不可解析：{DOCKER_MEMORY_FLAG}"
    value, unit = int(match.group(1)), match.group(2)
    assert (value * 1024 if unit == "g" else value) == SANDBOX_MEMORY_LIMIT_MB


def test_fact_sheet_partitions_stay_extractable() -> None:
    """文档比对吃的是机器抽取的事实源；抽取失败或分区缺失，文档就只能靠回忆写。"""
    import sys

    sys.path.insert(0, str(REPO))
    from scripts.engine_fact_sheet import (
        checkpoint_schema,
        provider_facts,
        sandbox_facts,
        trail_facts,
    )

    sandbox = sandbox_facts()
    assert sandbox["defaults_reference_constants"] and all(
        sandbox["defaults_reference_constants"].values()
    ), sandbox["defaults_reference_constants"]
    assert sandbox["stray_numeric_defaults"] == []
    assert sandbox["memory_enforced_in_subprocess"] in (True, False)  # 运行时探测，不是文本推断

    trail = trail_facts()
    assert trail["event_types"] == ["model_call", "tool_call", "artifact", "manual_edit"], (
        f"F-007 的四类留痕事件被改动：{trail['event_types']}"
    )
    assert trail["record_methods"], "留痕写入方法抽取为空"
    assert trail["store_uses_vocab"], "留痕存储不再引用脱敏词表"
    assert trail["delete_or_purge_api_present"] is False, (
        "audit 存储出现了 DELETE/DROP/purge —— F-007 留痕不可关闭被破坏"
    )
    assert trail["vocab_trail"] and trail["vocab_checkpoints"], "脱敏词表抽取为空，守护失去比对对象"

    assert provider_facts()["default_provider"] in provider_facts()["known_providers"]
    assert checkpoint_schema()["dbs"], "建表语句抽取为空"


def test_two_redaction_vocabularies_keep_their_documented_difference() -> None:
    """两套词表各有下限与"那一条刻意的不同"，都被调用方指着（②类漂移）。

    消费方：留痕 `trail/store.py:88` 传 TRAIL_*；检查点 `orchestrator/graph.py:432,445` 走
    默认 SENSITIVE_*。条目减少＝悄悄放宽脱敏，且不会有别的测试变红；把裸 "token" 加回
    TRAIL_* 则会连带抹掉 total_tokens，把 F-002 用量留痕清成空值。两种都在这条里钉住。
    """
    from engine.trail.redact import SENSITIVE_FRAGMENTS

    for name, vocab in (("SENSITIVE", SENSITIVE_FRAGMENTS), ("TRAIL", TRAIL_SENSITIVE_FRAGMENTS)):
        assert len(vocab) >= 6, f"{name}_SENSITIVE_FRAGMENTS 只剩 {len(vocab)} 条：值级脱敏被削弱"
        assert all(isinstance(f, str) and f for f in vocab), f"{name} 词表含空/非字符串条目"

    common = {"key", "secret", "password", "credential", "authorization"}
    assert common <= set(SENSITIVE_FRAGMENTS) and common <= set(TRAIL_SENSITIVE_FRAGMENTS)
    assert {"access_token", "refresh_token", "auth_token"} <= set(TRAIL_SENSITIVE_FRAGMENTS)
    assert "token" in SENSITIVE_FRAGMENTS, "编排状态里的裸 token 键不再被剔除（检查点红线）"
    assert "token" not in TRAIL_SENSITIVE_FRAGMENTS, (
        "裸 token 进留痕词表会连带抹掉 total_tokens，F-002 用量统计失去依据"
    )
