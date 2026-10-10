"""证据落盘与报告渲染（SP1-7 §5 证据归档）。

证据目录（默认 docs/acceptance/sp1-7/）：
- 逐题 JSON：{business_id}.json（通道/usage/逐阶段耗时/产物哈希/失败归因/恢复标记）；
- summary.json：汇总统计与决策门结论；
- report.md：Markdown 基线报告（判定报告附录引用）。
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.orchestrator.graph import STAGES
from engine.regression.problems import RegressionProblem
from engine.regression.runner import AcceptanceReport, ProblemResult


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
            "success_rate_all": report.success_rate(),
            "success_rate_baseline": report.success_rate("fakellm"),
            "success_rate_real_key": report.success_rate("real_key"),
            "by_category_baseline": report.by_category("fakellm"),
            "failure_by_module_baseline": report.failure_by_module("fakellm"),
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


def render_markdown(report: AcceptanceReport, *, title: str = "SP1-7 回归基线报告") -> str:
    """渲染 Markdown 报告：总体/题型分布/失败归因/逐题明细。"""
    lines = [
        f"# {title}",
        "",
        f"> 生成时间：{_now_iso()} · 判定门结论：{report.decision()}",
        "",
        "## 1. 总体",
        "",
        f"- 题目总数：{len(report.results)}",
        f"- 通过：{sum(1 for r in report.results if r.passed)}",
        f"- 全量成功率：{report.success_rate():.2%}",
        f"- FakeLLM 通道成功率：{report.success_rate('fakellm'):.2%}"
        "（回归护栏，不计判定分母，DEC-024）",
        f"- 真实 Key 通道成功率：{report.success_rate('real_key'):.2%}",
        "",
        "## 2. 题型分布（FakeLLM 通道）",
        "",
        "| 题型 | 总数 | 通过 |",
        "|---|---|---|",
    ]
    for category, stat in report.by_category("fakellm").items():
        lines.append(f"| {category} | {stat['total']} | {stat['passed']} |")
    lines += [
        "",
        "## 3. 失败归因（FakeLLM 通道）",
        "",
    ]
    failures = report.failure_by_module("fakellm")
    if not failures:
        lines.append("无失败样本。")
    else:
        lines += ["| 模块 | 数量 |", "|---|---|"]
        for module, count in sorted(failures.items()):
            lines.append(f"| {module} | {count} |")
    lines += [
        "",
        "## 4. 逐题明细",
        "",
        "| 题目 | 题型 | 通道 | 结果 | 归因 | 恢复 | 阶段耗时合计(ms) | prompt tokens | "
        "completion tokens | 产物哈希 | 门禁评分 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report.results:
        lines.append(
            f"| {r.business_id} | {r.category} | {r.channel} | {'通过' if r.passed else '失败'} "
            f"| {r.failure_module or '-'} | {'是' if r.resumed else '-'} | {r.duration_ms} "
            f"| {r.prompt_tokens} | {r.completion_tokens} | {r.paper_sha256 or '-'} "
            f"| {_scores_cell(r)} |"
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
