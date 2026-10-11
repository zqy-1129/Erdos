"""交付文档里的 `path:line` 引用必须真的指得到（"引用即校验"从口头纪律变成守护）。

起因不是理论风险，是本仓反复发生过的两类事故：
- 自家文档引用了**不存在的守护测试文件**（技术设计与 `native_backend.py` 注释各一处，
  10-11 审计当场查出）——给出一个看起来可信、实际永远不会跑的判据，比不写判据更糟；
- **P2 重写后行号整体漂移**（`answer_gate` 242→296、`_persist_*` 403/414→427/438…），
  对齐报告 §4.3 把"08 §14.2 / 10 W11"的锚点全部标记为失效，只能靠人肉逐条对。

本守护管**仓库内 tracked reports** 这一侧：文件存在 + 行号不越界。它测不到"行号存在但指的
不是那件事"（语义漂移仍需人读），也管不到飞书正文（那边由带日期增量维护）。
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REPORTS = REPO / "reports"

# 只认仓库内五个顶层根；前置否定环视防止从 `data-platform/content/scripts/...` 里
# 截出内层的 `scripts/...`——第一版就是这么误报了一条完全正确的引用。
REF = re.compile(
    r'(?<![\w./\-])((?:engine|scripts|client|server|contracts)/[\w./\-]+\.(?:py|ts|json|toml|md)):(\d+)'
)
MIN_REFERENCES = 25  # 少于这个数说明扫描根走错了，守护在空转


def _resolve(text: str, root: Path) -> list[str]:
    """返回不可解析的引用描述（空列表=全部指得到）。"""
    problems: list[str] = []
    for match in REF.finditer(text):
        rel, line_no = match.group(1), int(match.group(2))
        target = root / rel
        if not target.exists():
            problems.append(f"{rel}:{line_no} → 文件不存在")
            continue
        lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        if line_no > lines:
            problems.append(f"{rel}:{line_no} → 行号越界（该文件 {lines} 行）")
    return problems


def test_every_reference_in_tracked_reports_resolves() -> None:
    files = sorted(REPORTS.glob("*.md"))
    assert files, f"没找到任何交付报告：{REPORTS}"
    problems: list[str] = []
    total = 0
    for report in files:
        text = report.read_text(encoding="utf-8")
        total += len(REF.findall(text))
        for problem in _resolve(text, REPO):
            problems.append(f"{report.name}: {problem}")

    assert total >= MIN_REFERENCES, f"只扫到 {total} 条引用（下限 {MIN_REFERENCES}）——扫描根或正则走错了"
    assert not problems, "交付文档里有指不到的引用：\n" + "\n".join(problems)


def test_guard_actually_detects_dead_and_out_of_range_references() -> None:
    """正向对照：守护必须能被测坏，否则它只是在给自己发 ✅。

    两个坏样本各覆盖一种失效形态——文件不存在（改名/删除后没同步文档）、
    行号越界（文件被截短或引用凭空写大）。
    """
    missing = _resolve("见 engine/does_not_exist_guard.py:12", REPO)
    assert len(missing) == 1 and "文件不存在" in missing[0], missing

    out_of_range = _resolve("锚点 engine/ipc/methods.py:999999", REPO)
    assert len(out_of_range) == 1 and "行号越界" in out_of_range[0], out_of_range

    ok = _resolve("锚点 engine/ipc/methods.py:1 与 scripts/quality_gate.py:1", REPO)
    assert ok == [], ok


def test_ellipsis_paths_are_not_mistaken_for_references() -> None:
    """反例守护：写在括号里的省略号路径不该被当成引用而误报。

    第一版正则在 `data-platform/content/scripts/...py` 里截出内层 `scripts/...py`，
    把一条完全正确的引用判成"文件不存在"。**误报的守护会被关掉**，比没有守护更坏。
    """
    text = "同一条告警自报 `data-platform/content/scripts/cumcm_delivery/complex_formats.py:35`"
    assert _resolve(text, REPO) == []
