"""证据落盘与报告渲染（SP1-7 §5 证据归档）。

证据目录（默认 docs/acceptance/sp1-7/）：
- 逐题 JSON：{business_id}.json（通道/usage/逐阶段耗时/产物哈希/失败归因/恢复标记）；
- summary.json：汇总统计、整集冻结指纹（`seed_set`）与决策门结论；
- report.md：Markdown 基线报告（判定报告附录引用）。
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.orchestrator.graph import STAGES
from engine.regression.problems import RegressionProblem, manifest
from engine.regression.runner import (
    CHANNEL_BASELINE,
    CHANNEL_REAL,
    AcceptanceReport,
    ProblemResult,
)


class EvidenceWriter:
    """逐题证据与汇总报告落盘（写入失败上抛：证据缺失不可静默）。"""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def write_result(self, problem: RegressionProblem, result: ProblemResult) -> Path:
        payload: dict[str, Any] = {
            "generated_at": _now_iso(),
            "problem": problem.to_seed_record(),
            "result": asdict(result),
        }
        path = self._root / f"{problem.business_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def write_summary(
        self,
        report: AcceptanceReport,
        title: str = "SP1-7 回归基线报告",
        extra: dict[str, Any] | None = None,
    ) -> tuple[Path, Path]:
        """写入 summary.json 与 report.md；返回两个文件路径。

        extra：本轮评审口径与评委用量等运行级信息（如 gate_mode / judge_usage），
        由驱动脚本如实登记，判定报告附录可直接引用。
        """
        summary = {
            "generated_at": _now_iso(),
            "total": len(report.results),
            "passed": sum(1 for r in report.results if r.passed),
            # 分母与比率同框：没有 samples 这一栏，"success_rate_real_key: 0.0" 就分不清
            # "真实通道全挂"与"本批根本没跑真实通道"（DEC-024 的判定门恰好靠这个区别）。
            "samples": {
                "all": report.sample_count(),
                "fakellm": report.sample_count(CHANNEL_BASELINE),
                "real_key": report.sample_count(CHANNEL_REAL),
            },
            "success_rate_all": report.success_rate(),
            "success_rate_baseline": report.success_rate(CHANNEL_BASELINE),
            "success_rate_real_key": report.success_rate(CHANNEL_REAL),
            "by_category_baseline": report.by_category(CHANNEL_BASELINE),
            "failure_by_module_baseline": report.failure_by_module(CHANNEL_BASELINE),
            # 整集冻结指纹（G46①）：`manifest()` 不传参 = 取 `REGRESSION_SET` 全集，
            # **不是本次跑的那批**。冒烟跑（--per-category）也要能证明样本没变，
            # 否则校验依据只在全量跑时存在，而 11-05 之后恰恰主要靠冒烟跑。
            # docstring 早就承诺"判定报告附录引用"，此前生产链路零调用方。
            "seed_set": manifest(),
            "decision": str(report.decision()),
            **(extra or {}),
        }
        summary_path = self._root / "summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_path = self._root / "report.md"
        report_path.write_text(render_markdown(report, title=title), encoding="utf-8")
        return summary_path, report_path


def _path_distribution(results: list[ProblemResult]) -> str:
    """路径/隔离模式分布（"100% 通过"必须能读出它是怎么通过的，G41 证据面）。"""
    def tally(pick) -> str:  # noqa: ANN001 - 局部小取值函数
        counts: dict[str, int] = {}
        for r in results:
            key = pick(r) or "未知"
            counts[key] = counts.get(key, 0) + 1
        return "、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无样本"

    return f"求解路径 {tally(lambda r: r.solve_path)} · 沙箱隔离 {tally(lambda r: r.isolation_mode)}"


def _path_cell(r: ProblemResult) -> str:
    parts = [r.solve_path or "未知"]
    if r.dispatch_count is not None:
        parts.append(f"派发{r.dispatch_count}")
    parts.append(f"隔离:{r.isolation_mode or '未知'}")
    return "·".join(parts)


def _channel_split(report: AcceptanceReport) -> str:
    """通道分桶计数（通过/总数；零样本明写"无样本"）。"""
    parts = []
    for channel in (CHANNEL_BASELINE, CHANNEL_REAL):
        total = report.sample_count(channel)
        parts.append(
            f"{channel} {report.passed_count(channel)}/{total}"
            if total
            else f"{channel} 无样本"
        )
    return " · ".join(parts)


def _rate_line(
    label: str, report: AcceptanceReport, channel: str | None, note: str = ""
) -> str:
    """成功率一律带分母；零样本通道写"无样本"，不写 0.00%。

    `success_rate()` 在零样本时返回 0.0（含义是"没测过"），照原样渲染就得到
    "真实 Key 通道成功率：0.00%"——在判定报告里这会被读成"真实通道全挂"，而实际是本批
    只跑了 FakeLLM 护栏。缺证据一律记"未测量"，不补一个看起来合理的数（DEC-024 的判定门
    恰恰依赖这两者的区别）。
    """
    total = report.sample_count(channel)
    if not total:
        return f"- {label}：—（无样本，未测量）{note}"
    return f"- {label}：{report.success_rate(channel):.2%}（{report.passed_count(channel)}/{total}）{note}"


def _seed_set_lines(report: AcceptanceReport) -> list[str]:
    """整集冻结指纹 + 「本次 M 题 / 整集 N 题」标注（G46①③）。

    冒烟跑（`--per-category`）默认写进同一个证据目录，会用 4 题报告覆盖 20 题基线；没有这行
    标注，被覆盖后的 `report.md` 与全量跑出来的看起来一模一样（只有题数不同，而题数正好是
    唯一可能被读成"抽样比例"却没人写的东西）。指纹取**整集**，所以冒烟跑同样能证明样本没变。
    """
    seed = manifest()
    ran = report.sample_count()
    if ran == seed["count"]:
        scope = f"本次 {ran} 题 = 整集 {seed['count']} 题（全量）"
    elif ran == 0:
        # 与 §1 的比率同一口径：零样本要写"无样本"，"抽样 0 题"会被读成一次做了却什么都没中的抽样
        scope = f"本次**无样本**（未跑任何题）/ 整集 {seed['count']} 题"
    else:
        scope = f"**本次抽样 {ran} 题 / 整集 {seed['count']} 题**"
    buckets = "、".join(f"{c} {n}" for c, n in seed["categories"].items())
    return [
        f"- 题目总数：{ran}（{scope}）",
        f"- 整集冻结指纹：`set_sha256={seed['set_sha256']}`"
        "（逐题哈希见 `summary.json` 的 `seed_set`；11-05 冻结后据此校验样本未变）",
        f"- 整集题型分桶：{buckets}",
    ]


def render_markdown(report: AcceptanceReport, *, title: str = "SP1-7 回归基线报告") -> str:
    """渲染 Markdown 报告：总体/题型分布/失败归因/逐题明细。"""
    baseline_total = report.sample_count(CHANNEL_BASELINE)
    lines = [
        f"# {title}",
        "",
        f"> 生成时间：{_now_iso()} · 判定门结论：{report.decision()}",
        "",
        "## 1. 总体",
        "",
        *_seed_set_lines(report),
        f"- 通过：{report.passed_count()}（{_channel_split(report)}）",
        f"- **{_path_distribution(report.results)}**"
        "（厂商不支持 tools 时全套会静默降级，成功率不变但工具链从未被验过）",
        _rate_line("全量成功率", report, None),
        _rate_line(
            "FakeLLM 通道成功率", report, CHANNEL_BASELINE,
            "（回归护栏，不计判定分母，DEC-024）",
        ),
        _rate_line("真实 Key 通道成功率", report, CHANNEL_REAL),
        "",
        "## 2. 题型分布（FakeLLM 通道）",
        "",
    ]
    if not baseline_total:
        # 空表在这里会被读成"该题型零失败"，而真实含义是"根本没跑这个通道"
        lines.append("本批 FakeLLM 通道**无样本**——本节不适用，不是零失败。")
    else:
        lines += ["| 题型 | 总数 | 通过 |", "|---|---|---|"]
        for category, stat in report.by_category(CHANNEL_BASELINE).items():
            lines.append(f"| {category} | {stat['total']} | {stat['passed']} |")
    lines += [
        "",
        "## 3. 失败归因（FakeLLM 通道）",
        "",
    ]
    failures = report.failure_by_module(CHANNEL_BASELINE)
    if not baseline_total:
        lines.append("本批 FakeLLM 通道**无样本**：这里不是「无失败」，而是没测过。")
    elif not failures:
        lines.append(f"无失败样本（FakeLLM 通道 {baseline_total} 题全部通过）。")
    else:
        lines += ["| 模块 | 数量 |", "|---|---|"]
        for module, count in sorted(failures.items()):
            lines.append(f"| {module} | {count} |")
    lines += [
        "",
        "## 4. 逐题明细",
        "",
        "| 题目 | 题型 | 通道 | 结果 | 归因 | 恢复 | 阶段耗时合计(ms) | prompt tokens | "
        "completion tokens | 产物哈希 | 门禁评分 | 求解路径·派发·隔离 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report.results:
        lines.append(
            f"| {r.business_id} | {r.category} | {r.channel} | {'通过' if r.passed else '失败'} "
            f"| {r.failure_module or '-'} | {'是' if r.resumed else '-'} | {r.duration_ms} "
            f"| {r.prompt_tokens} | {r.completion_tokens} | {r.paper_sha256 or '-'} "
            f"| {_scores_cell(r)} | {_path_cell(r)} |"
        )

    scored = [r for r in report.results if r.gate_scores]
    if scored:
        lines += [
            "",
            "## 5. rubric 评审明细",
            "",
            "每格为「加权总分/阈值（评委调用次数）」；空格表示该阶段未进入评审。",
            "",
            "| 题目 | " + " | ".join(STAGES) + " |",
            "|---" * (len(STAGES) + 1) + "|",
        ]
        for r in scored:
            by_stage = {g.stage: g for g in r.gate_scores}
            cells = [
                (
                    f"{by_stage[s].total_score:.2f}/{by_stage[s].threshold:.2f}"
                    f"（{by_stage[s].attempts}）{'✓' if by_stage[s].passed else '✗'}"
                )
                if s in by_stage
                else " "
                for s in STAGES
            ]
            lines.append(f"| {r.business_id} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _scores_cell(result: ProblemResult) -> str:
    """逐题明细的门禁评分摘要（无 rubric 分数时标注评审口径）。"""
    if not result.gate_scores:
        return "自动通过（无 rubric 分数）"
    return " ".join(
        f"{g.stage[:4]} {g.total_score:.2f}{'✓' if g.passed else '✗'}"
        for g in result.gate_scores
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
