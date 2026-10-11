"""沙箱阈值单源守护（W37 · EN-THRESH-SSOT 的第一批：G32 的沙箱行）。

G32 里最容易被低估的一条是"同一个承诺有两种写法"：PRD §6.1 说单次执行 ≤120s、≤2GB，代码里
一处写 `2048`、另一处写 `"2g"`，还有三个 `timeout: float = 120.0` 散在两个沙箱与工厂函数上。
数字本身没错，错在**改一处不会带上另一处**——而漏改的那处不报错，只会让某条路径上的资源承诺
静默失效（与 `stream_options`、`provider` 矩阵键同族：都是"看起来是一回事，实际各写一遍"）。

这里锁三件事：
① 两种内存表示必须仍是同一个值（互证，不是各写一遍）；
② 沙箱默认参数必须引用常量，源码里不得残留裸字面量（AST/文本双查，防止"常量加了、默认值忘了改"）；
③ 常量本身等于 PRD §6.1 的承诺数值（PRD 不在仓库里，所以判据是"测试侧字面量 + 出处"，
   一旦 PRD 改动，红的是这条断言，而不是靠人记得去改代码）。
"""

import re
from pathlib import Path

from engine.sandbox.constants import (
    DOCKER_MEMORY_FLAG,
    SANDBOX_MEMORY_LIMIT_MB,
    SANDBOX_TIMEOUT_SECONDS,
)
from engine.sandbox.docker_sandbox import DockerSandbox
from engine.sandbox.subprocess_sandbox import SubprocessSandbox

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_DIR = REPO_ROOT / "engine" / "sandbox"

# PRD §6.1 的承诺数值（仓库内无 PRD 副本，出处即飞书《Erdos PRD》§6.1「执行隔离」）
PRD_TIMEOUT_SECONDS = 120.0
PRD_MEMORY_LIMIT_MB = 2048


def _docker_flag_to_mb(flag: str) -> int:
    """把 Docker 的 `2g` / `512m` 规格换成 MB（两种表示必须能互证，不是各写一遍）。"""
    match = re.fullmatch(r"(\d+)([gm])", flag.strip().lower())
    assert match, f"Docker 内存规格格式不可解析：{flag!r}"
    value, unit = int(match.group(1)), match.group(2)
    return value * 1024 if unit == "g" else value


def test_two_memory_notations_are_the_same_value() -> None:
    assert _docker_flag_to_mb(DOCKER_MEMORY_FLAG) == SANDBOX_MEMORY_LIMIT_MB, (
        f"`{DOCKER_MEMORY_FLAG}` 与 {SANDBOX_MEMORY_LIMIT_MB}MB 已经不是一个值了——"
        "改一处漏一处时，Docker 路径与 subprocess 路径会各自执行不同的承诺"
    )


def test_constants_match_prd_promise() -> None:
    assert SANDBOX_TIMEOUT_SECONDS == PRD_TIMEOUT_SECONDS, (
        "PRD §6.1 承诺单次执行 ≤120s：常量漂移要么改 PRD 要么改代码，不能只改一边"
    )
    assert SANDBOX_MEMORY_LIMIT_MB == PRD_MEMORY_LIMIT_MB


def test_sandbox_defaults_reference_constants() -> None:
    """默认值必须走常量；源码里残留裸字面量就是"加了常量但没接完"。"""
    for name in ("subprocess_sandbox.py", "docker_sandbox.py"):
        source = (SANDBOX_DIR / name).read_text(encoding="utf-8")
        stray = re.findall(
            r"(?:timeout: float|memory_limit_mb: int|memory: str) = (?!SANDBOX_|DOCKER_)[\d\"']+",
            source,
        )
        assert not stray, f"{name} 仍有裸字面量默认值：{stray}"


def test_constructed_sandboxes_carry_the_constants() -> None:
    """实例上的生效值（不是源码文本）——防止常量导入了却被调用方覆盖掉而无人知。"""
    subprocess_box = SubprocessSandbox()
    assert subprocess_box._timeout == SANDBOX_TIMEOUT_SECONDS  # noqa: SLF001 - 断言生效值
    assert subprocess_box._memory_limit_mb == SANDBOX_MEMORY_LIMIT_MB  # noqa: SLF001

    docker_box = DockerSandbox()
    assert docker_box._timeout == SANDBOX_TIMEOUT_SECONDS  # noqa: SLF001
    assert docker_box._memory == DOCKER_MEMORY_FLAG  # noqa: SLF001


def test_fact_sheet_reports_thresholds_and_refs() -> None:
    """事实源必须报出阈值与"默认值是否已引用常量"，并把残留裸字面量列出来。

    文档比对拿的是这份 JSON；事实源漏一项或半覆盖，比拿到错误结论更难发现（本批已踩过一次）。
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.engine_fact_sheet import sandbox_facts

    facts = sandbox_facts()
    assert facts["thresholds"]["timeout_seconds"] == SANDBOX_TIMEOUT_SECONDS
    assert facts["thresholds"]["memory_limit_mb"] == SANDBOX_MEMORY_LIMIT_MB
    assert facts["thresholds"]["docker_memory_flag"] == DOCKER_MEMORY_FLAG
    assert all(facts["defaults_reference_constants"].values()), facts["defaults_reference_constants"]
    assert facts["stray_numeric_defaults"] == [], facts["stray_numeric_defaults"]
