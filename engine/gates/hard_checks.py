"""门禁硬检查（SP1-3）：阶段产出的代码级判定——无 LLM、无副作用、可单测。

边界：只做「执行状态 + 产物结构」判定（语义评分归 rubric LLM 评委，见 evaluator.py）；
违规说明为「状态+原因」式短句，可直接进 gate.failed.reason，禁止携带堆栈。
"""

import math
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

MAX_MESSAGE_LEN = 120

_PAPER_SECTIONS = ("## 摘要", "## 一、问题重述", "## 三、求解与结果", "## 四、结论")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def check_stage(stage: str, data: dict) -> list[str]:
    """返回违规说明列表（空列表 = 通过）；未知阶段也计违规（不得静默放行）。"""
    checker = _CHECKERS.get(stage)
    if checker is None:
        return [_msg(f"未知阶段 {stage or '（空）'}：无对应硬检查规则，不能默认通过")]
    return checker(data)


def _msg(text: str) -> str:
    return text[:MAX_MESSAGE_LEN]


def _check_analysis(data: dict) -> list[str]:
    insights = data.get("insights")
    if not isinstance(insights, list) or not insights:
        return [_msg("分析阶段 insights 缺失或不是非空数组，无可用分析要点")]
    if not any(str(x).strip() for x in insights):
        return [_msg("分析阶段 insights 条目全为空白，无可用分析要点")]
    return []


def _check_modeling(data: dict) -> list[str]:
    violations: list[str] = []
    for field in ("assumptions", "modeling_detail"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            violations.append(_msg(f"建模阶段 {field} 缺失或为空白文本"))
    variables = data.get("variables")
    if not isinstance(variables, list) or not variables:
        violations.append(_msg("建模阶段 variables 缺失或不是非空数组"))
    return violations


def _check_solving(data: dict) -> list[str]:
    """mode 缺失或非 tool_loop 时按阶段级规则判——**这不是反推路径，而是 fail-closed 选择**。

    评审时一度改成"mode 不认识就报违规"，实测后撤回：
    - 阶段级规则要求 `exit_code == 0` 且有输出/产物，工具循环的 data 缺这些字段时照样判违规，
      所以默认分支已经是"不放行"，改成报错只是换了条更精确的文案；
    - 而 W23② 之前落盘的 stage_level 检查点本来就没有 `mode`，恢复重挂门禁时会被新文案误判，
      换来的是开发机上的一次无谓重跑。缺字段反推路径那个形状在这里不成立（另有正向用例把
      "默认分支不放过空数据"钉住，见 test_hard_checks 的 stage_level 用例）。
    """
    if data.get("mode") == "tool_loop":
        return _check_solving_tool_loop(data)
    return _check_solving_stage_level(data)


def _check_solving_tool_loop(data: dict) -> list[str]:
    violations: list[str] = []
    status = data.get("status")
    if status != "succeeded":
        violations.append(_msg(f"求解阶段工具循环未成功收敛（status={status}）"))
    results = data.get("results")
    if not isinstance(results, list) or not results:
        violations.append(_msg("求解阶段工具循环 results 缺失或不是非空数组"))
    else:
        for item in results:
            value = item.get("value") if isinstance(item, dict) else None
            if isinstance(value, (int, float)) and not math.isfinite(value):
                name = item.get("name") if isinstance(item, dict) else ""
                violations.append(_msg(f"求解结果 {name or '(未命名)'} 为 NaN/无穷（value={value}），数值不可用"))
    for field in ("repair_count", "dispatch_count"):
        value = data.get(field)
        if not isinstance(value, int):
            violations.append(_msg(f"求解阶段 {field} 非整数（value={_short(value)}）"))
    return violations


def _check_solving_stage_level(data: dict) -> list[str]:
    violations: list[str] = []
    if data.get("timed_out"):
        violations.append(_msg("求解阶段沙箱执行超时（timed_out=True），结果不完整"))
    exit_code = data.get("exit_code")
    if exit_code != 0:
        violations.append(_msg(f"求解阶段脚本退出码非 0（exit_code={_short(exit_code)}）"))
    stdout = data.get("stdout")
    artifacts = data.get("artifacts")
    has_stdout = isinstance(stdout, str) and bool(stdout.strip())
    has_artifacts = isinstance(artifacts, list) and bool(artifacts)
    if not has_stdout and not has_artifacts:
        violations.append(_msg("求解阶段 stdout 与 artifacts 同时为空，脚本无输出亦无产物"))
    return violations


def _check_writing(data: dict) -> list[str]:
    violations: list[str] = []
    paper = data.get("paper_md")
    if not isinstance(paper, str) or not paper.strip():
        violations.append(_msg("报告阶段 paper_md 缺失或为空白文本"))
    else:
        missing = [section for section in _PAPER_SECTIONS if section not in paper]
        if missing:
            violations.append(_msg(f"报告阶段论文缺章节：{'、'.join(missing)}"))
    digest = data.get("paper_sha256")
    declared: str | None = digest if isinstance(digest, str) and _SHA256_RE.match(digest) else None
    if declared is None:
        violations.append(_msg(f"报告阶段 paper_sha256 非 64 位十六进制小写（value={_short(digest)}）"))
    path = data.get("paper_path")
    if not isinstance(path, str) or not path.strip():
        violations.append(_msg("报告阶段 paper_path 缺失，产物文件无从校验"))
        return violations
    if not Path(path).exists():
        violations.append(_msg(f"报告阶段产物文件不存在（paper_path={_short(path)}）"))
        return violations
    # 声明与实际产物必须是同一个东西：只验格式 + 存在，留着"阶段记录一个哈希、
    # artifact_index/合规声明另一个哈希"的窗口——审计一比对就像产物被改过。
    if declared is not None:
        actual = sha256(Path(path).read_bytes()).hexdigest()
        if actual != declared:
            violations.append(
                _msg(f"报告阶段 paper_sha256 与产物文件不一致（声明 {declared[:12]}…，实际 {actual[:12]}…）")
            )
    return violations


def _short(value: Any) -> str:  # noqa: ANN401 - 任意字段值的展示用截断
    return repr(value)[:40]


_CHECKERS = {
    "analysis": _check_analysis,
    "modeling": _check_modeling,
    "solving": _check_solving,
    "writing": _check_writing,
}
